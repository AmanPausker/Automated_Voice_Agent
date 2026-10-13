# Automated Voice Receptionist

AI-powered voice agent for clinics and hospitals built with **Pipecat**, **FastAPI**, **Deepgram**, **Cartesia**, **OpenRouter / OpenAI**, **WhatsApp Cloud API**, and **Per-Doctor Google Calendar Scheduling** (with Cal.com fallback).

---

## 1. Architecture Overview

```
                      +-----------------------------+
                      |   Inbound Caller / Telephony |
                      |    (Vobiz DID / Web Audio)  |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |      FastAPI WebSocket      |
                      |   (/ws/vobiz-stream, /ws)   |
                      +--------------+--------------+
                                     |
                                     v
                      +-----------------------------+
                      |    Pipecat Voice Pipeline   |
                      |  Silero VAD -> Deepgram STT |
                      |   -> LLM -> Cartesia TTS    |
                      +--------------+--------------+
                                     |
        +----------------------------+----------------------------+
        | Function Calling                                        |
        v                                                         v
+-------------------------+                           +-------------------------+
|     Clinic Timings      |                           |   Live Slot Discovery   |
|   `get_clinic_timings`  |                           | `check_available_slots` |
+-------------------------+                           +------------+------------+
                                                                   |
                                          +------------------------+------------------------+
                                          |                                                 |
                                          v                                                 v
                              [ Google Calendar Mode ]                             [ Cal.com Mode ]
                              - Multi-Doctor Personal Calendars                    - Cal.com v2 API
                              - Pre-seeded "Green" Available Slots                 - Fallback provider
                              - Live Freebusy & Overlap Check
                              - Conflict-free ETag & Lock Booking
                                          |
                                          v
                      +---------------------------------------+
                      | SQLite (agent_data.db)                |
                      | - doctors (encrypted refresh tokens)  |
                      | - clinic_config (hours, breaks)       |
                      | - doctor_slots (seeded/removed/booked)|
                      | - appointments & calendar_sync_state  |
                      | - notification_outbox & audit_log     |
                      +-------------------+-------------------+
                                          |
                                          v
                      +---------------------------------------+
                      | Meta WhatsApp Cloud API (Outbox)      |
                      | Automated BOOKED / RESCHEDULE / CANCEL|
                      +---------------------------------------+
```

---

## 2. Google Cloud Setup Guide (Per-Doctor Personal Gmail)

### Step 2.1: Enable Google Calendar API
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project or select your existing project.
3. In the navigation menu, go to **APIs & Services** > **Library**.
4. Search for **Google Calendar API** and click **Enable**.

### Step 2.2: Configure OAuth Consent Screen
1. Go to **APIs & Services** > **OAuth consent screen**.
2. Select **External** user type and click **Create**.
3. Fill in App Name (e.g. `Clinic Voice Receptionist`), User support email, and Developer contact information.
4. Under **Scopes**, click **Add or Remove Scopes** and add:
   - `https://www.googleapis.com/auth/calendar.events` (Manage events on doctor calendars)
5. **Testing Mode & Test Users (Crucial):**
   - While your Google app is in **Testing** status, only added test users can log in (up to 100 users).
   - Under **Test users**, click **Add Users** and add each doctor's personal Gmail address (e.g., `doctor1@gmail.com`).
   - *Production Note:* The `calendar.events` scope is sensitive; deploying beyond your 100 test users requires submitting the app for Google OAuth verification.

### Step 2.3: Create OAuth 2.0 Credentials
1. Go to **APIs & Services** > **Credentials**.
2. Click **Create Credentials** > **OAuth client ID**.
3. Application type: **Web application**.
4. Name: `Clinic Voice Receptionist Backend`.
5. **Authorized redirect URIs**:
   - `http://localhost:8000/oauth/google/callback` (Local development)
   - `https://<your-production-domain>/oauth/google/callback` (Production / ngrok)
6. Click **Create** and copy your **Client ID** and **Client Secret**.

---

## 3. Environment Configuration

Add the following to your `.env` file (see `.env.example`):

```bash
# Scheduling Engine Provider ('google' | 'calcom', default is 'calcom')
SCHEDULING_PROVIDER=google

# Google OAuth 2.0
GOOGLE_CLIENT_ID=your_google_client_id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your_google_client_secret
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/oauth/google/callback
GOOGLE_TOKEN_ENCRYPTION_KEY=sui7yD6M1azMZ0PHOTqqUW0ktdhZYKJstzFB2b7q--w=

# Clinic Timings & Slot Rules
CLINIC_TIMEZONE=Asia/Kolkata
CLINIC_WORKING_DAYS=[1,2,3,4,5,6]
CLINIC_OPEN_TIME=09:00
CLINIC_CLOSE_TIME=18:00
CLINIC_BREAK_WINDOWS=[{"start":"13:00","end":"14:00"}]
CLINIC_HOLIDAYS=[]
SLOT_SEED_WINDOW_DAYS=30

# Google Colors (2 = Sage/Green for Available slots)
GOOGLE_SLOT_COLOR_ID=2
GOOGLE_APPOINTMENT_COLOR_IDS={}
```

To generate a new Fernet encryption key:
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 4. Doctor Onboarding Workflow

1. Open the interactive dashboard at `http://localhost:8000/dashboard` or visit:
   ```
   http://localhost:8000/oauth/google/start?doctor_id=dr_smith&name=Dr.+John+Smith&specialty=Cardiology
   ```
2. The doctor is redirected to Google OAuth consent screen and approves calendar access for their personal Gmail account.
3. The server exchanges the authorization code for a refresh token, encrypts it using Fernet (`GOOGLE_TOKEN_ENCRYPTION_KEY`), and saves the doctor to the database with `status="active"`.
4. Initial "Green" available slots are seeded automatically for the next 30 days based on clinic timings and break windows.
5. Nightly rolling background worker (`_slot_seed_worker`) ensures a rolling 30-day window is maintained.
6. If a doctor deletes or shortens a slot directly in Google Calendar, the system tracks the deletion in `doctor_slots` with state `removed` and **never resurrects it**.

---

## 5. Migration Guide: Switching from Cal.com to Google Calendar

The system includes a zero-downtime dual-provider architecture:

1. **Step 1 (Parallel Run):**
   Leave `SCHEDULING_PROVIDER=calcom` in `.env`. Connect your doctors to Google Calendar via `/oauth/google/start`. Test Google slot queries directly at `/availability?provider=google`.
2. **Step 2 (Cutover):**
   Change `SCHEDULING_PROVIDER=google` in `.env` and restart the server.
   The voice agent immediately queries live Google Calendar slots, books appointments with per-slot conflict locks, and converts slots into appointments.
3. **Step 3 (Rollback if needed):**
   Change `SCHEDULING_PROVIDER=calcom` to revert instantly without losing any appointment logs.

---

## 6. Running the Application & Tests

### Start Server
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Run Tests
```bash
.venv/bin/python -m unittest discover tests
```
All 17 tests (WhatsApp outbox notifications + Google Calendar slot seeding, conflict prevention, live overlap check, cancellation, and token refresh) will run and pass.
