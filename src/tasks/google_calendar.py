"""
Google Calendar integration for doctor availability seeding, live slot calculation,
conflict-free booking, cancellation, rescheduling, and push sync.
"""

import os
import json
import uuid
import hashlib
import asyncio
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple
from loguru import logger
from dotenv import load_dotenv

import googleapiclient.discovery
from googleapiclient.errors import HttpError

from db.database import (
    get_doctor,
    list_doctors,
    get_clinic_config,
    record_slot_state,
    get_slot_state,
    save_appointment,
    get_appointment,
    update_appointment_status,
    save_sync_state,
    get_sync_state,
    log_audit_event,
)
from src.utils.google_auth import get_doctor_credentials, GoogleAuthNeedsReconnectError
from src.utils.whatsapp import (
    WHATSAPP_TENANT_ID,
    WHATSAPP_TIMEZONE,
    build_booking_template,
    is_valid_e164_phone,
)

load_dotenv()

GOOGLE_SLOT_COLOR_ID = os.getenv("GOOGLE_SLOT_COLOR_ID", "2")  # '2' = Sage (Green in Google Calendar)
GOOGLE_DEFAULT_APPOINTMENT_COLOR_ID = "1"  # '1' = Lavender

# Map of doctor_id -> color_id (can be passed via JSON env var)
def _get_appointment_color_ids() -> Dict[str, str]:
    raw = os.getenv("GOOGLE_APPOINTMENT_COLOR_IDS", "{}")
    try:
        return json.loads(raw)
    except Exception:
        return {}

SLOT_SEED_WINDOW_DAYS = int(os.getenv("SLOT_SEED_WINDOW_DAYS", "30"))

# In-memory slot locks to prevent double-booking on single instance
_SLOT_LOCKS: Dict[str, asyncio.Lock] = {}
_LOCK_GLOBAL = asyncio.Lock()


async def _get_slot_lock(doctor_id: str, slot_start_utc: str) -> asyncio.Lock:
    key = f"{doctor_id}:{slot_start_utc}"
    async with _LOCK_GLOBAL:
        if key not in _SLOT_LOCKS:
            _SLOT_LOCKS[key] = asyncio.Lock()
        return _SLOT_LOCKS[key]


def get_deterministic_event_id(doctor_id: str, slot_start_utc: str) -> str:
    """
    Generate a deterministic, lowercase hex ID valid for Google Calendar events [a-v0-9]{5,1024}.
    """
    raw = f"{doctor_id}:{slot_start_utc}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def get_calendar_service(doctor_id: str):
    """
    Build and return authenticated Google Calendar Resource for a doctor.
    """
    creds = get_doctor_credentials(doctor_id)
    if not creds:
        raise ValueError(f"No valid credentials found for doctor {doctor_id}")
    return googleapiclient.discovery.build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_time_str(time_str: str) -> time:
    """Parse 'HH:MM' string to time object."""
    parts = time_str.split(":")
    return time(hour=int(parts[0]), minute=int(parts[1]))


def generate_candidate_slots(
    doctor_id: str,
    days: int = 30,
) -> List[Dict[str, Any]]:
    """
    Generate candidate slot start/end UTC datetimes for a doctor based on clinic configuration,
    excluding holidays, non-working days, and break windows.
    """
    doctor = get_doctor(doctor_id)
    if not doctor:
        return []

    tenant_id = doctor.get("tenant_id", "default")
    config = get_clinic_config(tenant_id=tenant_id, doctor_id=doctor_id)

    tz_name = config.get("timezone") or doctor.get("timezone") or "Asia/Kolkata"
    tz = ZoneInfo(tz_name)

    slot_minutes = int(doctor.get("slot_duration_minutes") or 15)
    working_days = set(config.get("working_days", [1, 2, 3, 4, 5, 6]))
    open_time = _parse_time_str(config.get("open_time", "09:00"))
    close_time = _parse_time_str(config.get("close_time", "18:00"))
    break_windows = [
        (_parse_time_str(b["start"]), _parse_time_str(b["end"]))
        for b in config.get("break_windows", [])
    ]
    holidays = set(config.get("holidays", []))

    now_local = datetime.now(tz)
    start_date = now_local.date()

    candidates = []

    for day_offset in range(days):
        curr_date = start_date + timedelta(days=day_offset)
        iso_weekday = curr_date.isoweekday()  # 1 = Mon ... 7 = Sun
        date_str = curr_date.strftime("%Y-%m-%d")

        if iso_weekday not in working_days:
            continue
        if date_str in holidays:
            continue

        curr_time = open_time
        while True:
            slot_start_dt = datetime.combine(curr_date, curr_time, tzinfo=tz)
            slot_end_dt = slot_start_dt + timedelta(minutes=slot_minutes)

            # Check if exceeded close time
            if slot_end_dt.time() > close_time and slot_end_dt.date() == curr_date:
                break

            # Skip past slots
            if slot_start_dt <= now_local:
                curr_time = (slot_start_dt + timedelta(minutes=slot_minutes)).time()
                continue

            # Check break windows
            in_break = False
            for b_start, b_end in break_windows:
                if not (slot_end_dt.time() <= b_start or slot_start_dt.time() >= b_end):
                    in_break = True
                    break

            if not in_break:
                slot_start_utc = slot_start_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                slot_end_utc = slot_end_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                candidates.append({
                    "doctor_id": doctor_id,
                    "slot_start_local": slot_start_dt,
                    "slot_end_local": slot_end_dt,
                    "slot_start_utc": slot_start_utc,
                    "slot_end_utc": slot_end_utc,
                    "timezone": tz_name,
                    "tenant_id": tenant_id,
                })

            curr_time = (slot_start_dt + timedelta(minutes=slot_minutes)).time()

    return candidates


async def seed_slots_for_doctor(doctor_id: str, days: int = SLOT_SEED_WINDOW_DAYS) -> Dict[str, Any]:
    """
    Seed Google Calendar with green 'Available' slots for a doctor.
    Idempotent: Never resurrects doctor-deleted slots and does not duplicate existing slots.
    """
    doctor = get_doctor(doctor_id)
    if not doctor or doctor.get("status") != "active":
        return {"status": "skipped", "reason": "Doctor inactive or not found"}

    try:
        service = get_calendar_service(doctor_id)
    except Exception as exc:
        logger.warning(f"Cannot build calendar service for doctor {doctor_id}: {exc}")
        return {"status": "error", "error": str(exc)}

    candidates = generate_candidate_slots(doctor_id, days=days)
    calendar_id = doctor.get("calendar_id", "primary")

    seeded_count = 0
    skipped_count = 0

    for slot in candidates:
        slot_start_utc = slot["slot_start_utc"]
        slot_end_utc = slot["slot_end_utc"]

        # Check existing state in SQLite
        existing_state = get_slot_state(doctor_id, slot_start_utc)
        if existing_state:
            # Rule: Never resurrect a slot a doctor deliberately removed
            if existing_state["state"] in ("removed", "booked", "seeded"):
                skipped_count += 1
                continue

        event_id = get_deterministic_event_id(doctor_id, slot_start_utc)

        event_body = {
            "id": event_id,
            "summary": "Available",
            "description": "Bookable slot managed by Automated Voice Receptionist",
            "colorId": GOOGLE_SLOT_COLOR_ID,
            "transparency": "transparent",  # Does not block freebusy by default
            "start": {
                "dateTime": slot["slot_start_local"].isoformat(),
                "timeZone": slot["timezone"],
            },
            "end": {
                "dateTime": slot["slot_end_local"].isoformat(),
                "timeZone": slot["timezone"],
            },
            "extendedProperties": {
                "private": {
                    "type": "slot",
                    "managedBy": "clinic-system",
                    "doctorId": doctor_id,
                    "tenantId": slot["tenant_id"],
                }
            },
        }

        try:
            # Attempt insert
            service.events().insert(
                calendarId=calendar_id,
                body=event_body,
            ).execute(num_retries=3)

            record_slot_state(
                doctor_id=doctor_id,
                slot_start=slot_start_utc,
                slot_end=slot_end_utc,
                google_event_id=event_id,
                state="seeded",
            )
            seeded_count += 1
        except HttpError as exc:
            if exc.resp.status == 409:
                # Already exists in Google Calendar
                record_slot_state(
                    doctor_id=doctor_id,
                    slot_start=slot_start_utc,
                    slot_end=slot_end_utc,
                    google_event_id=event_id,
                    state="seeded",
                )
                skipped_count += 1
            else:
                logger.error(f"Error inserting slot for doctor {doctor_id}: {exc}")

    log_audit_event(
        event_type="SEED",
        doctor_id=doctor_id,
        details={"seeded": seeded_count, "skipped": skipped_count, "days": days},
    )
    return {
        "status": "success",
        "doctor_id": doctor_id,
        "seeded": seeded_count,
        "skipped": skipped_count,
    }


def _iso_to_datetime(dt_str: str) -> datetime:
    """Parse ISO datetime string (with or without timezone) into timezone-aware datetime in UTC."""
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def get_available_slots_google(
    doctor_or_specialty: Optional[str] = None,
    date_str: Optional[str] = None,
    timezone_name: str = "Asia/Kolkata",
) -> List[Dict[str, Any]]:
    """
    Compute live bookable slots for a given date across matching doctors.
    Live check: Green available slots that have no overlapping opaque/personal events.
    """
    if not date_str:
        # Default to tomorrow
        date_str = (datetime.now(ZoneInfo(timezone_name)).date() + timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        target_date = datetime.now(ZoneInfo(timezone_name)).date() + timedelta(days=1)
        date_str = target_date.strftime("%Y-%m-%d")

    tz = ZoneInfo(timezone_name)
    day_start_local = datetime.combine(target_date, time(0, 0, 0), tzinfo=tz)
    day_end_local = datetime.combine(target_date, time(23, 59, 59), tzinfo=tz)

    day_start_utc = day_start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_end_utc = day_end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Match doctors
    all_doctors = list_doctors()
    matched_doctors: List[Dict[str, Any]] = []

    if doctor_or_specialty:
        target_norm = doctor_or_specialty.strip().lower()
        for doc in all_doctors:
            if (
                target_norm in doc.get("name", "").lower()
                or target_norm in doc.get("doctor_id", "").lower()
                or target_norm in doc.get("specialty", "").lower()
            ):
                matched_doctors.append(doc)

    if not matched_doctors:
        matched_doctors = [d for d in all_doctors if d.get("status") == "active"]

    if not matched_doctors:
        return []

    available_slots: List[Dict[str, Any]] = []

    for doctor in matched_doctors:
        doctor_id = doctor["doctor_id"]
        if doctor.get("status") != "active":
            continue

        try:
            service = get_calendar_service(doctor_id)
        except Exception as exc:
            logger.warning(f"Skipping doctor {doctor_id} availability check: {exc}")
            continue

        calendar_id = doctor.get("calendar_id", "primary")

        try:
            events_result = service.events().list(
                calendarId=calendar_id,
                timeMin=day_start_utc,
                timeMax=day_end_utc,
                singleEvents=True,
                orderBy="startTime",
            ).execute(num_retries=2)
            events = events_result.get("items", [])
        except Exception as exc:
            logger.error(f"Failed to fetch live events for doctor {doctor_id}: {exc}")
            continue

        green_slots = []
        blocking_intervals = []

        for evt in events:
            if evt.get("status") == "cancelled":
                continue

            start_raw = evt.get("start", {}).get("dateTime") or evt.get("start", {}).get("date")
            end_raw = evt.get("end", {}).get("dateTime") or evt.get("end", {}).get("date")
            if not start_raw or not end_raw:
                continue

            # Convert to UTC datetime
            start_utc = _iso_to_datetime(start_raw)
            end_utc = _iso_to_datetime(end_raw)

            ext_props = evt.get("extendedProperties", {}).get("private", {})
            event_type = ext_props.get("type")
            is_managed = ext_props.get("managedBy") == "clinic-system"

            if is_managed and event_type == "slot" and evt.get("summary") == "Available":
                green_slots.append({
                    "event": evt,
                    "start_utc": start_utc,
                    "end_utc": end_utc,
                    "google_event_id": evt["id"],
                })
            else:
                # Any non-managed event or managed appointment event blocks time
                blocking_intervals.append((start_utc, end_utc))

        for slot in green_slots:
            slot_s = slot["start_utc"]
            slot_e = slot["end_utc"]
            slot_s_str = slot_s.strftime("%Y-%m-%dT%H:%M:%SZ")

            # Check if doctor deliberately deleted this slot in DB
            db_state = get_slot_state(doctor_id, slot_s_str)
            if db_state and db_state.get("state") == "removed":
                continue

            # Check overlap with any blocking event
            has_overlap = False
            for b_s, b_e in blocking_intervals:
                if not (slot_e <= b_s or slot_s >= b_e):
                    has_overlap = True
                    break

            if not has_overlap:
                slot_local = slot_s.astimezone(tz)
                time_str = slot_local.strftime("%I:%M %p").lstrip("0")
                available_slots.append({
                    "doctor_id": doctor_id,
                    "doctor_name": doctor.get("name", "Doctor"),
                    "specialty": doctor.get("specialty", ""),
                    "start_time": slot_s_str,
                    "display_time": time_str,
                    "date": date_str,
                    "google_event_id": slot["google_event_id"],
                })

    return available_slots


async def book_appointment_google(
    doctor_id: str,
    slot_start: str,
    patient_name: str,
    phone: str,
    email: Optional[str] = None,
    reason: Optional[str] = None,
    call_id: Optional[str] = None,
    tenant_id: str = "default",
) -> Dict[str, Any]:
    """
    Book an appointment against Google Calendar by converting a green slot into a booked appointment.
    Guarded with per-slot lock and live ETag check to eliminate double-booking.
    """
    lock = await _get_slot_lock(doctor_id, slot_start)
    async with lock:
        doctor = get_doctor(doctor_id)
        if not doctor:
            return {"status": "error", "message": f"Doctor {doctor_id} not found"}

        try:
            service = get_calendar_service(doctor_id)
        except Exception as exc:
            return {"status": "error", "message": f"Calendar authorization error: {exc}"}

        calendar_id = doctor.get("calendar_id", "primary")
        event_id = get_deterministic_event_id(doctor_id, slot_start)

        # 1. Fetch live event state
        try:
            live_event = service.events().get(
                calendarId=calendar_id,
                eventId=event_id,
            ).execute(num_retries=2)
        except HttpError as exc:
            if exc.resp.status == 404:
                return {
                    "status": "conflict",
                    "code": 409,
                    "message": "Selected time slot is no longer available. Please select another slot.",
                }
            logger.error(f"Error checking slot {event_id}: {exc}")
            return {"status": "error", "message": "Failed to verify slot availability"}

        # Verify event is still an open green slot
        ext_props = live_event.get("extendedProperties", {}).get("private", {})
        if (
            live_event.get("summary") != "Available"
            or ext_props.get("type") != "slot"
            or live_event.get("transparency") == "opaque"
        ):
            return {
                "status": "conflict",
                "code": 409,
                "message": "This slot was just booked by another patient. Please choose a different time.",
            }

        appointment_id = f"gcal_{uuid.uuid4().hex[:12]}"
        appt_color_map = _get_appointment_color_ids()
        appt_color_id = appt_color_map.get(doctor_id) or doctor.get("color_id") or GOOGLE_DEFAULT_APPOINTMENT_COLOR_ID
        if appt_color_id == GOOGLE_SLOT_COLOR_ID:
            appt_color_id = GOOGLE_DEFAULT_APPOINTMENT_COLOR_ID

        start_dt = _iso_to_datetime(live_event["start"].get("dateTime") or slot_start)
        end_dt = _iso_to_datetime(live_event["end"].get("dateTime") or slot_start)

        # 2. Update the slot into an opaque booked appointment
        updated_event = {
            "summary": f"Appointment - {patient_name}",
            "description": (
                f"Patient Name: {patient_name}\n"
                f"Phone: {phone}\n"
                f"Email: {email or 'N/A'}\n"
                f"Reason: {reason or 'General consultation'}\n"
                f"Appointment ID: {appointment_id}\n"
                f"Call ID: {call_id or 'N/A'}"
            ),
            "colorId": appt_color_id,
            "transparency": "opaque",
            "start": live_event["start"],
            "end": live_event["end"],
            "extendedProperties": {
                "private": {
                    "type": "appointment",
                    "managedBy": "clinic-system",
                    "appointmentId": appointment_id,
                    "doctorId": doctor_id,
                    "patientName": patient_name,
                    "patientPhone": phone,
                    "tenantId": tenant_id,
                }
            },
        }

        try:
            service.events().update(
                calendarId=calendar_id,
                eventId=event_id,
                body=updated_event,
            ).execute(num_retries=3)
        except HttpError as exc:
            if exc.resp.status in (409, 412):
                return {
                    "status": "conflict",
                    "code": 409,
                    "message": "Conflict during booking. Slot is already taken.",
                }
            logger.error(f"Failed to update calendar event {event_id}: {exc}")
            return {"status": "error", "message": "Failed to finalize booking with calendar"}

        # 3. Update DB & queue WhatsApp notification
        start_utc_str = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc_str = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        notification_template = None
        if phone and is_valid_e164_phone(phone):
            try:
                notification_template = build_booking_template(
                    patient_name=patient_name,
                    start_time=start_utc_str,
                    appointment_id=appointment_id,
                )
            except Exception as exc:
                logger.warning(f"Could not build WhatsApp template: {exc}")

        save_appointment(
            appointment_data={
                "appointment_id": appointment_id,
                "doctor_id": doctor_id,
                "google_event_id": event_id,
                "patient_name": patient_name,
                "phone": phone,
                "reason": reason,
                "start_time": start_utc_str,
                "end_time": end_utc_str,
                "status": "booked",
                "call_id": call_id,
                "tenant_id": tenant_id,
            },
            notification_template=notification_template,
        )

        record_slot_state(
            doctor_id=doctor_id,
            slot_start=start_utc_str,
            slot_end=end_utc_str,
            google_event_id=event_id,
            state="booked",
        )

        log_audit_event(
            event_type="BOOKED",
            entity_id=appointment_id,
            doctor_id=doctor_id,
            details={
                "patient": patient_name,
                "phone": phone,
                "slot_start": start_utc_str,
                "call_id": call_id,
            },
        )

        return {
            "status": "success",
            "appointment_id": appointment_id,
            "doctor_id": doctor_id,
            "doctor_name": doctor.get("name"),
            "slot_start": start_utc_str,
            "patient_name": patient_name,
            "message": f"Appointment booked successfully with {doctor.get('name')}.",
        }


async def cancel_appointment_google(
    appointment_id: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Cancel an appointment and revert the Google Calendar event back to a green 'Available' slot.
    """
    appt = get_appointment(appointment_id)
    if not appt:
        return {"status": "error", "message": f"Appointment {appointment_id} not found"}

    doctor_id = appt["doctor_id"]
    doctor = get_doctor(doctor_id)
    if not doctor:
        return {"status": "error", "message": f"Doctor {doctor_id} not found"}

    service = get_calendar_service(doctor_id)
    calendar_id = doctor.get("calendar_id", "primary")
    event_id = appt["google_event_id"]

    try:
        live_event = service.events().get(
            calendarId=calendar_id,
            eventId=event_id,
        ).execute(num_retries=2)

        # Revert back to Available
        reverted_event = {
            "summary": "Available",
            "description": "Bookable slot managed by Automated Voice Receptionist",
            "colorId": GOOGLE_SLOT_COLOR_ID,
            "transparency": "transparent",
            "start": live_event["start"],
            "end": live_event["end"],
            "extendedProperties": {
                "private": {
                    "type": "slot",
                    "managedBy": "clinic-system",
                    "doctorId": doctor_id,
                    "tenantId": appt.get("tenant_id", "default"),
                }
            },
        }

        service.events().update(
            calendarId=calendar_id,
            eventId=event_id,
            body=reverted_event,
        ).execute(num_retries=3)
    except Exception as exc:
        logger.warning(f"Could not revert event {event_id} in Google Calendar: {exc}")

    # Build cancellation WhatsApp notification template if applicable
    notification_template = None
    if appt.get("phone") and is_valid_e164_phone(appt["phone"]):
        try:
            notification_template = build_booking_template(
                patient_name=appt["patient_name"],
                start_time=appt["start_time"],
                appointment_id=appointment_id,
            )
        except Exception:
            pass

    update_appointment_status(
        appointment_id=appointment_id,
        status="cancelled",
        notification_template=notification_template,
    )

    record_slot_state(
        doctor_id=doctor_id,
        slot_start=appt["start_time"],
        slot_end=appt["end_time"],
        google_event_id=event_id,
        state="seeded",
    )

    log_audit_event(
        event_type="CANCELLED",
        entity_id=appointment_id,
        doctor_id=doctor_id,
        details={"reason": reason},
    )

    return {
        "status": "success",
        "appointment_id": appointment_id,
        "message": "Appointment cancelled and slot reopened.",
    }


async def reschedule_appointment_google(
    appointment_id: str,
    new_slot_start: str,
) -> Dict[str, Any]:
    """
    Reschedule an appointment to a new slot:
    1. Revert the old slot back to Available.
    2. Book the new slot.
    3. Update the appointment record.
    """
    appt = get_appointment(appointment_id)
    if not appt:
        return {"status": "error", "message": f"Appointment {appointment_id} not found"}

    doctor_id = appt["doctor_id"]
    patient_name = appt["patient_name"]
    phone = appt["phone"]
    email = ""
    reason = appt.get("reason", "")
    call_id = appt.get("call_id")
    tenant_id = appt.get("tenant_id", "default")

    # 1. Book the new slot first
    book_result = await book_appointment_google(
        doctor_id=doctor_id,
        slot_start=new_slot_start,
        patient_name=patient_name,
        phone=phone,
        email=email,
        reason=reason,
        call_id=call_id,
        tenant_id=tenant_id,
    )

    if book_result.get("status") != "success":
        return book_result

    # 2. Free up the old slot
    old_event_id = appt["google_event_id"]
    try:
        doctor = get_doctor(doctor_id)
        if doctor:
            service = get_calendar_service(doctor_id)
            live_old = service.events().get(
                calendarId=doctor.get("calendar_id", "primary"),
                eventId=old_event_id,
            ).execute(num_retries=2)

            service.events().update(
                calendarId=doctor.get("calendar_id", "primary"),
                eventId=old_event_id,
                body={
                    "summary": "Available",
                    "description": "Bookable slot managed by Automated Voice Receptionist",
                    "colorId": GOOGLE_SLOT_COLOR_ID,
                    "transparency": "transparent",
                    "start": live_old["start"],
                    "end": live_old["end"],
                    "extendedProperties": {
                        "private": {
                            "type": "slot",
                            "managedBy": "clinic-system",
                            "doctorId": doctor_id,
                            "tenantId": tenant_id,
                        }
                    },
                },
            ).execute(num_retries=2)
    except Exception as exc:
        logger.warning(f"Error reverting old slot {old_event_id} during reschedule: {exc}")

    record_slot_state(
        doctor_id=doctor_id,
        slot_start=appt["start_time"],
        slot_end=appt["end_time"],
        google_event_id=old_event_id,
        state="seeded",
    )

    # 3. Update old appointment record status or link
    update_appointment_status(
        appointment_id=appointment_id,
        status="rescheduled",
        new_start_time=new_slot_start,
    )

    log_audit_event(
        event_type="RESCHEDULED",
        entity_id=appointment_id,
        doctor_id=doctor_id,
        details={
            "old_start": appt["start_time"],
            "new_start": new_slot_start,
            "new_appointment_id": book_result.get("appointment_id"),
        },
    )

    return {
        "status": "success",
        "appointment_id": book_result.get("appointment_id"),
        "old_appointment_id": appointment_id,
        "new_start_time": new_slot_start,
        "message": "Appointment rescheduled successfully.",
    }


# ---------------------------------------------------------------------------
# Google Calendar Webhook Watch Registration & Incremental Sync (Sync Token)
# ---------------------------------------------------------------------------

async def watch_doctor_calendar(doctor_id: str, webhook_url: str) -> Dict[str, Any]:
    """
    Register a push notification watch on the doctor's calendar.
    """
    doctor = get_doctor(doctor_id)
    if not doctor or doctor.get("status") != "active":
        return {"status": "skipped", "message": "Doctor inactive"}

    service = get_calendar_service(doctor_id)
    calendar_id = doctor.get("calendar_id", "primary")

    channel_id = f"chan_{doctor_id}_{uuid.uuid4().hex[:8]}"
    secret_token = f"token_{doctor_id}_{uuid.uuid4().hex[:8]}"

    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": webhook_url,
        "token": secret_token,
    }

    try:
        watch_res = service.events().watch(
            calendarId=calendar_id,
            body=body,
        ).execute(num_retries=3)

        resource_id = watch_res.get("resourceId")
        expiration = watch_res.get("expiration")

        save_sync_state(
            doctor_id=doctor_id,
            channel_id=channel_id,
            resource_id=resource_id,
            channel_expiration=str(expiration),
        )

        return {
            "status": "success",
            "channel_id": channel_id,
            "resource_id": resource_id,
            "expiration": expiration,
        }
    except Exception as exc:
        logger.error(f"Failed to watch calendar for doctor {doctor_id}: {exc}")
        return {"status": "error", "error": str(exc)}


async def process_calendar_sync(doctor_id: str) -> Dict[str, Any]:
    """
    Incremental sync using syncToken. Detects manual doctor deletions and updates slot state.
    On 410 GONE, falls back to full resync.
    """
    doctor = get_doctor(doctor_id)
    if not doctor or doctor.get("status") != "active":
        return {"status": "skipped"}

    try:
        service = get_calendar_service(doctor_id)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    calendar_id = doctor.get("calendar_id", "primary")
    sync_state = get_sync_state(doctor_id) or {}
    sync_token = sync_state.get("sync_token")

    params: Dict[str, Any] = {"calendarId": calendar_id}
    if sync_token:
        params["syncToken"] = sync_token

    try:
        events_res = service.events().list(**params).execute(num_retries=2)
    except HttpError as exc:
        if exc.resp.status == 410:
            logger.warning(f"Sync token expired for doctor {doctor_id}. Performing full resync.")
            params.pop("syncToken", None)
            events_res = service.events().list(**params).execute(num_retries=2)
        else:
            logger.error(f"Sync error for doctor {doctor_id}: {exc}")
            return {"status": "error", "error": str(exc)}

    items = events_res.get("items", [])
    for evt in items:
        status = evt.get("status")
        evt_id = evt.get("id")

        if status == "cancelled":
            # Doctor or system deleted the event in Google Calendar
            # If this was a seeded slot, mark as 'removed' so seeder NEVER resurrects it
            ext_props = evt.get("extendedProperties", {}).get("private", {})
            start_raw = evt.get("start", {}).get("dateTime")
            if start_raw:
                start_utc = _iso_to_datetime(start_raw).strftime("%Y-%m-%dT%H:%M:%SZ")
                record_slot_state(
                    doctor_id=doctor_id,
                    slot_start=start_utc,
                    slot_end="",
                    google_event_id=evt_id,
                    state="removed",
                )

    next_sync_token = events_res.get("nextSyncToken")
    if next_sync_token:
        save_sync_state(doctor_id=doctor_id, sync_token=next_sync_token)

    log_audit_event(
        event_type="SYNC",
        doctor_id=doctor_id,
        details={"items_processed": len(items)},
    )
    return {"status": "success", "processed": len(items)}
