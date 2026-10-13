# Prompt: Replace Cal.com with per-doctor Google Calendar scheduling

> Copy everything below this line into your coding agent. It is written to be executed **inside the existing `Automated_Voice_Agent` repository**, not as a new app.

---

You are a senior Python backend engineer working inside an existing production repo called `Automated_Voice_Agent`. Your job is to **add per-doctor Google Calendar scheduling to this project** and gradually replace the current Cal.com integration. Do **not** scaffold a new project, do not switch stacks, and do not "rewrite the app clean". Extend what already exists.

## 0. Existing project — read this before writing code

**Stack (do not change):**
- Python 3.11, FastAPI, served by Uvicorn. Single entrypoint: `main.py`.
- Voice pipeline: **Pipecat** (`pipecat-ai`). Pipeline built in `src/agent/pipeline.py` via `create_agent_pipeline(transport, call_id, caller_phone)`.
- Telephony: **Vobiz** media-stream WebSocket (`/ws/vobiz-stream`) plus a zero-cost browser test WebSocket (`/ws/browser-call`) and dashboard at `/dashboard`.
- STT Deepgram Nova-2, LLM OpenRouter or OpenAI (`gpt-4o-mini`), TTS Cartesia (OpenAI TTS fallback).
- Scheduling today: **Cal.com v2** in `src/tasks/cal_booking.py` (`list_event_types`, `get_available_slots`, `book_appointment`).
- DB: **SQLite** via `db/database.py` (`get_db_connection()` contextmanager, `init_db()` at import time with additive `ALTER TABLE` migrations). Existing tables: `calls`, `bookings`, `whatsapp_preferences`, `notification_outbox`.
- Notifications: **WhatsApp Cloud API** outbox pattern (`notification_outbox` table) drained by `_notification_worker()` in `main.py`'s `lifespan`.
- Config: `python-dotenv`, `.env` / `.env.example`. Secrets never logged.
- Logging: `loguru`. HTTP: `httpx`.
- Tests: `unittest.IsolatedAsyncioTestCase` under `tests/`, patching `httpx.AsyncClient` with a fake client (see `tests/test_whatsapp_notifications.py` for the house style).
- Agent tools are Pipecat `FunctionSchema` objects registered with `llm.register_function(name, handler)`, where the handler is `async def handler(params: FunctionCallParams)` and replies through `await params.result_callback({...})`.
- The LLM persona/system prompt lives in `src/agent/prompts.py` (`get_system_prompt(caller_name, caller_phone)`).

**Conventions to follow:**
- Keep modules small and single-purpose, mirroring `src/tasks/`, `src/utils/`, `src/user/`.
- Add new DB columns/tables with **additive, idempotent** migrations inside `init_db()` — never drop or rewrite existing tables. Existing local `agent_data.db` files must keep working.
- Add new env vars to both `.env` and `.env.example`.
- Reuse the existing `notification_outbox` + WhatsApp worker for booking/reschedule/cancel confirmations instead of building a second notifier.
- All times stored in UTC; displayed in the clinic timezone (`WHATSAAP_TIMEZONE`, currently `Asia/Kolkata`).
- Return structured dicts from task functions, and never raise raw provider errors into the voice pipeline.

## 1. Confirmed design decisions (these are answers to open questions — implement them, don't re-litigate)

1. **Google Calendar must work on doctors' personal Gmail accounts (e.g. `doctor@gmail.com`).** This is supported: each doctor grants your app OAuth 2.0 consent, and you store their refresh token. Caveat to document in the README: while your Google Cloud OAuth consent screen is in **Testing** mode only added test users can authorize (100-user cap), and the `calendar.events` scope is **sensitive/restricted**, so production use requires Google verification.
2. **The server can create/update/delete events on each doctor's personal calendar** using the stored refresh token, including setting the per-event `colorId` (Google event colors 1–11) and doctor-specific start/end slot times. The server can only manipulate **events** with the `calendar.events` scope — it cannot change the doctor's account-level calendar list/colors. Therefore doctor identity is expressed with a **per-event `colorId`** plus `extendedProperties`, not by recoloring the calendar itself.
3. **Free ("green") availability slots are pre-seeded by the server** from clinic timings, and **doctors can freely CRUD those events directly in Google Calendar.** The server must detect those manual edits (push notifications + incremental sync) and treat deleted/shortened green slots as unavailable, and any non-managed event the doctor adds as blocked time.
4. **The voice agent answers clinic-hours questions from clinic config and books only against live calendar availability.** Clinic timings come from config; actual bookable slots come from the calendar state.

## 2. Goal

Make Google Calendar the single source of truth for doctor availability, and make the Pipecat voice agent expose the right tools to query clinic timings, check availability, and book/reschedule/cancel — all integrated into the existing FastAPI app, DB, and WhatsApp notification flow.

## 3. Requirements, module by module

### 3.1 Dependencies & config
- Add to `requirements.txt`: `google-api-python-client`, `google-auth`, `google-auth-oauthlib`, `google-auth-httplib2`, `cryptography` (Fernet for token encryption).
- New env vars (add to `.env.example`, never commit real secrets):
  - `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`
  - `GOOGLE_TOKEN_ENCRYPTION_KEY` (Fernet key for refresh tokens)
  - `SCHEDULING_PROVIDER` (`google` | `calcom`, default `calcom` so nothing breaks during migration)
  - `CLINIC_TIMEZONE`, `CLINIC_WORKING_DAYS`, `CLINIC_OPEN_TIME`, `CLINIC_CLOSE_TIME`, `CLINIC_BREAK_WINDOWS`, `CLINIC_HOLIDAYS`
  - `GOOGLE_SLOT_COLOR_ID` (green, default `2` = Sage), `GOOGLE_APPOINTMENT_COLOR_IDS` (JSON map doctor→colorId)
  - `SLOT_SEED_WINDOW_DAYS` (default 30), `GOOGLE_PUBSUB_TOPIC` / webhook base URL for `events.watch`.

### 3.2 Database (additive migrations in `db/database.py`)
Add tables (idempotent `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` guards, matching existing style):
- `doctors`: `doctor_id` (PK), `name`, `email`, `encrypted_refresh_token`, `calendar_id` (default `"primary"`), `color_id`, `slot_duration_minutes`, `timezone`, `specialty`, `status` (`active` | `needs_reconnect`), `created_at`, `updated_at`.
- `clinic_config`: per-tenant working days, open/close, break windows, holidays (JSON), timezone. Optional per-doctor overrides.
- `doctor_slots`: track seeded slot windows and deliberate deletions so the seeder is idempotent and never resurrects a slot a doctor removed (`doctor_id`, `slot_start` UTC, `slot_end` UTC, `google_event_id`, `state` `seeded|removed|booked`, `updated_at`).
- `calendar_sync_state`: `doctor_id`, `sync_token`, `channel_id`, `resource_id`, `channel_expiration`, `updated_at` (for incremental sync + webhook channel renewal).
- `appointments`: `appointment_id`, `doctor_id`, `google_event_id`, `patient_name`, `phone`, `reason`, `start_time` UTC, `end_time` UTC, `status` (`booked|cancelled|rescheduled`), `call_id`, `tenant_id`, `created_at`.
- Reuse `bookings` for legacy Cal.com rows; write new Google bookings to `appointments` and, where a WhatsApp confirmation is queued, keep using `notification_outbox` (`event_type` already supports `BOOKED|RESCHEDULED|CANCELLED`).

Add accessor functions in `db/database.py` (same signature style as `save_booking`, `set_whatsapp_preference`): `upsert_doctor`, `get_doctor`, `set_doctor_status`, `save_sync_state`, `get_sync_state`, `save_appointment`, `get_appointment`, `update_appointment_status`, `record_slot_state`, `get_slot_state`.

### 3.3 Google OAuth (per doctor, personal Gmail)
New module `src/utils/google_auth.py`:
- Authorization-code flow with `access_type=offline`, `prompt=consent`, scope `https://www.googleapis.com/auth/calendar.events` (add `calendar.readonly` only if truly needed for freebusy).
- Endpoints in `main.py`: `GET /oauth/google/start?doctor_id=...` → redirect to consent; `GET /oauth/google/callback` → exchange code, encrypt and persist the refresh token, set doctor `status=active`.
- Build credentials with `google.oauth2.credentials.Credentials` and refresh automatically; if refresh fails with `invalid_grant`, set the doctor `status=needs_reconnect` and never crash a call.
- Encrypt refresh tokens at rest with Fernet using `GOOGLE_TOKEN_ENCRYPTION_KEY`. Never log tokens.

### 3.4 Slot seeding ("green available" events)
New module `src/tasks/google_calendar.py` (mirror `cal_booking.py` structure):
- `get_calendar_service(doctor_id)` → authenticated `googleapiclient.discovery.build("calendar", "v3", credentials=...)`.
- `seed_slots_for_doctor(doctor_id, days=SLOT_SEED_WINDOW_DAYS)`: generate slots from clinic config (or per-doctor override) × `slot_duration_minutes`, skipping breaks and holidays.
- Each slot event: summary `Available`, `start`/`end` in the doctor's timezone, `colorId = GOOGLE_SLOT_COLOR_ID` (green), `transparency = "transparent"`, and `extendedProperties.private = { type: "slot", managedBy: "clinic-system", doctorId, tenantId }`.
- **Idempotency:** use a deterministic Google event `id` derived from `hash(doctorId + slot_start)` (base32hex, lowercase) and/or check `extendedProperties` before insert; never duplicate. Use `new_batch_http_request()` for bulk inserts.
- **Never resurrect doctor-deleted slots:** consult `doctor_slots.state` (and the sync watcher) before recreating. Track seeded vs removed.
- Run seeding on doctor onboarding and via a **rolling nightly job** (30-day window). Wire it into the existing `lifespan` background pattern in `main.py` (like `_notification_worker`), e.g. `_slot_seed_worker()` that seeds then sleeps until the next run.

### 3.5 Doctor CRUD in Google Calendar + change detection
- Doctors may create/edit/move/delete events directly in Google Calendar. Do not overwrite doctor-created events.
- Register `events.watch` per doctor calendar; add `POST /webhooks/google/calendar` to receive push notifications. Validate the channel token; renew channels before `channel_expiration` (track in `calendar_sync_state`).
- On notification, run `events.list(syncToken=...)` incremental sync; persist the new `syncToken`. On `410 GONE`, drop the token and do a full resync.
- Rule set: a green slot that is deleted, shortened, or moved → unavailable. Any non-managed event overlapping a slot (personal event, leave, meeting, opaque event) → blocked. A slot converted to an appointment by the system → booked.

### 3.6 Availability + booking API
Keep the existing REST shapes and add Google-backed endpoints in `main.py` (do not remove the Cal.com endpoints until migration is done):
- `GET /availability?doctorId=&date=&specialty=` → compute open slots from **live calendar state** (`events.list` in the window, or `freebusy.query`), intersecting green slots with no overlapping opaque/managed-booked event. Do not answer purely from cache.
- `POST /appointments` `{ doctorId, slotStart, patientName, phone, reason }` → convert the green slot event into a booked appointment: summary `Appointment - {patientName}`, description with phone and reason, `transparency = "opaque"`, doctor-specific `colorId` (never green), `extendedProperties.private.type = "appointment"`. Update `appointments` + `doctor_slots` and queue the existing WhatsApp `BOOKED` notification when consent exists.
- Double-booking prevention: re-fetch the event, update with `If-Match` (ETag) / conditional request, return `409` on conflict, and guard with a per-slot lock (process-local `asyncio.Lock` keyed by `doctorId:slotStart`, matching the single-instance SQLite deployment).
- `POST /appointments/{id}/cancel` and `/reschedule` → revert the event to a green `Available` slot or move it; queue `CANCELLED` / `RESCHEDULED`.
- `GET /clinic-info` → clinic timings, holidays, address, per-doctor info.
- `GET /doctors` → doctor list with specialty + availability summary (for the dashboard and for the agent to resolve "which doctor?").

### 3.7 Voice agent integration (Pipecat)
- In `src/agent/pipeline.py`, add/replace tools built as Pipecat `FunctionSchema` objects and registered with `llm.register_function(...)`:
  - `get_clinic_timings()` — returns general clinic working hours, breaks, holidays, address.
  - `check_availability(doctor_or_specialty, date)` — returns up to 3–4 open slots for that date; may accept a date range and return the next available day when empty.
  - `book_appointment(doctor_id, slot_start, patient_name, phone, reason)` — books against the calendar.
  - `cancel_appointment(...)`, `reschedule_appointment(...)`.
- Names/signatures may map to the existing handlers to minimize churn: keep `book_appointment` and `check_available_slots` names if convenient, but route them to `src/tasks/google_calendar.py` based on `SCHEDULING_PROVIDER`.
- Update `get_system_prompt` in `src/agent/prompts.py`: greet the caller, answer clinic-hour questions **only** via `get_clinic_timings`, never invent availability, offer at most 3 slots at a time, confirm name/phone/date/time by reading them back before booking, when there are no slots offer the next available day, and speak times in the clinic timezone in natural language. Keep the existing no-markdown, 1–2 sentence, no-medical-details rules and the WhatsApp-consent flow.

### 3.8 WhatsApp confirmations
Reuse `notification_outbox` + `process_notification_queue()`. Queue confirmations on `BOOKED`, `RESCHEDULED`, `CANCELLED` only when the caller gave explicit consent and the number is valid E.164 (as today). Do not add a second notification path.

## 4. Non-functional requirements
- All times stored in UTC, rendered in the clinic timezone; handle DST correctly via `zoneinfo`.
- Exponential backoff on Google `403`/`429`/`5xx` rate limits (use client `num_retries` plus your own wrapper).
- Structured `loguru` logging + an audit trail for every booking, cancellation, reschedule, and calendar sync (new `audit_log` table or reuse `notification_outbox` patterns).
- Encrypt refresh tokens at rest; never log tokens or patient details beyond what's needed.
- Env-based config only; keep the `.env.example` current.
- Update the `/dashboard` HTML and the `/` health payload to show Google Calendar connection status and per-doctor slot state.

## 5. Testing (match `tests/test_whatsapp_notifications.py` style)
- `unittest.IsolatedAsyncioTestCase`, temp SQLite via `patch.object(database, "DB_PATH", ...)`, and a fake Google client / patched `httpx` or `discovery.build`.
- Cover: slot generation from clinic config (breaks + holidays + DST), seeding idempotency (no duplicates, no resurrection of removed slots), double-booking prevention (409), availability computed from live events, and OAuth token refresh + `needs_reconnect` on `invalid_grant`.

## 6. Deliverables & execution order
Present an architecture overview first, then implement in this order, module by module, keeping the app runnable at each step:
1. DB migrations + accessors in `db/database.py`.
2. `src/utils/google_auth.py` (OAuth + credential refresh + encryption).
3. `src/tasks/google_calendar.py` (service builder, slot seeding, availability, booking/cancel/reschedule).
4. REST endpoints in `main.py` (availability, appointments, clinic-info, doctors, OAuth, Google webhook) + background `_slot_seed_worker`.
5. `events.watch` + incremental sync handler.
6. Pipecat tools in `src/agent/pipeline.py` + system prompt in `src/agent/prompts.py`.
7. WhatsApp confirmation wiring for the new appointment events.
8. Tests + README section: Google Cloud setup (enable Calendar API, OAuth consent screen + test users while in Testing mode, redirect URIs, verification/scopes note), env template, and migration note for switching `SCHEDULING_PROVIDER` from `calcom` to `google`.

Start with the architecture overview and the DB schema, then implement in the order above. Do not delete the existing Cal.com code until the Google path is fully working and covered by tests.
