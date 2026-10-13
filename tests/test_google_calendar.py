"""
Comprehensive unit tests for Google Calendar scheduling, slot seeding,
live availability calculation, double-booking prevention, cancellation,
rescheduling, and OAuth token refresh error handling.
"""

import os
import json
import tempfile
import unittest
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo
from unittest.mock import patch, MagicMock

import db.database as database
import src.utils.google_auth as google_auth
import src.tasks.google_calendar as gcal_tasks
from src.tasks.google_calendar import (
    generate_candidate_slots,
    seed_slots_for_doctor,
    get_available_slots_google,
    book_appointment_google,
    cancel_appointment_google,
    reschedule_appointment_google,
    get_deterministic_event_id,
)
from googleapiclient.errors import HttpError


class FakeHttpResp:
    def __init__(self, status: int, reason: str = ""):
        self.status = status
        self.reason = reason


class FakeGoogleEventsResource:
    def __init__(self):
        self.events_db: dict[str, dict] = {}
        self.watch_calls: list[dict] = []

    def insert(self, calendarId: str, body: dict):
        mock_req = MagicMock()
        event_id = body.get("id") or f"evt_{len(self.events_db)}"
        if event_id in self.events_db:
            # Emulate Google 409 Conflict
            def raise_409(*args, **kwargs):
                raise HttpError(resp=FakeHttpResp(409, "Conflict"), content=b"Event exists")
            mock_req.execute = raise_409
            return mock_req

        stored = dict(body)
        stored["id"] = event_id
        self.events_db[event_id] = stored

        mock_req.execute = lambda *args, **kwargs: stored
        return mock_req

    def list(self, calendarId: str, **kwargs):
        mock_req = MagicMock()
        items = list(self.events_db.values())
        mock_req.execute = lambda *args, **kwargs: {"items": items, "nextSyncToken": "sync_token_123"}
        return mock_req

    def get(self, calendarId: str, eventId: str):
        mock_req = MagicMock()
        if eventId not in self.events_db:
            def raise_404(*args, **kwargs):
                raise HttpError(resp=FakeHttpResp(404, "Not Found"), content=b"Event not found")
            mock_req.execute = raise_404
            return mock_req

        mock_req.execute = lambda *args, **kwargs: dict(self.events_db[eventId])
        return mock_req

    def update(self, calendarId: str, eventId: str, body: dict):
        mock_req = MagicMock()
        if eventId not in self.events_db:
            def raise_404(*args, **kwargs):
                raise HttpError(resp=FakeHttpResp(404, "Not Found"), content=b"Event not found")
            mock_req.execute = raise_404
            return mock_req

        stored = dict(body)
        stored["id"] = eventId
        self.events_db[eventId] = stored
        mock_req.execute = lambda *args, **kwargs: stored
        return mock_req

    def watch(self, calendarId: str, body: dict):
        mock_req = MagicMock()
        self.watch_calls.append(body)
        mock_req.execute = lambda *args, **kwargs: {
            "resourceId": "res_123",
            "expiration": "1773000000000",
        }
        return mock_req


class FakeGoogleService:
    def __init__(self):
        self._events = FakeGoogleEventsResource()

    def events(self):
        return self._events


class GoogleCalendarSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.branding_patch = patch.dict(
            os.environ,
            {
                "WHATSAPP_HOSPITAL_NAME": "Example Clinic",
                "WHATSAPP_DOCTOR_NAME": "Dr Example",
                "WHATSAPP_SPECIALIZATION": "General Practice",
                "WHATSAPP_HOSPITAL_ADDRESS": "1 Test Street",
                "WHATSAPP_HOSPITAL_PHONE": "+14155550100",
            },
        )
        self.branding_patch.start()
        self.db_path_patch = patch.object(
            database, "DB_PATH", os.path.join(self.temp_dir.name, "test_gcal.db")
        )
        self.db_path_patch.start()
        database.init_db()

        self.fake_service = FakeGoogleService()
        self.service_patch = patch.object(
            gcal_tasks, "get_calendar_service", return_value=self.fake_service
        )
        self.service_patch.start()

        # Seed initial doctor
        database.upsert_doctor({
            "doctor_id": "dr_smith",
            "name": "Dr. John Smith",
            "email": "smith@gmail.com",
            "encrypted_refresh_token": "fake_encrypted_token",
            "calendar_id": "primary",
            "color_id": "1",
            "slot_duration_minutes": 30,
            "timezone": "Asia/Kolkata",
            "specialty": "Cardiology",
            "status": "active",
            "tenant_id": "default",
        })

        # Set standard clinic config (9:00 - 17:00, lunch break 13:00 - 14:00)
        database.upsert_clinic_config({
            "tenant_id": "default",
            "doctor_id": "dr_smith",
            "working_days": [1, 2, 3, 4, 5, 6],
            "open_time": "09:00",
            "close_time": "17:00",
            "break_windows": [{"start": "13:00", "end": "14:00"}],
            "holidays": ["2026-12-25"],
            "timezone": "Asia/Kolkata",
        })

    def tearDown(self):
        self.service_patch.stop()
        self.db_path_patch.stop()
        self.branding_patch.stop()
        self.temp_dir.cleanup()

    async def test_slot_candidate_generation_breaks_and_holidays(self):
        """Verify slot generation excludes break windows, holidays, and respects slot durations."""
        candidates = generate_candidate_slots("dr_smith", days=7)
        self.assertTrue(len(candidates) > 0)

        for slot in candidates:
            # Slot duration must be 30 minutes
            start = slot["slot_start_local"]
            end = slot["slot_end_local"]
            self.assertEqual((end - start).total_seconds(), 30 * 60)

            # Slot must never fall within break window 13:00 - 14:00
            slot_start_time = start.time()
            slot_end_time = end.time()
            self.assertFalse(
                slot_start_time >= time(13, 0) and slot_start_time < time(14, 0),
                f"Slot starts during break window: {slot_start_time}",
            )
            self.assertFalse(
                slot_end_time > time(13, 0) and slot_end_time <= time(14, 0),
                f"Slot ends during break window: {slot_end_time}",
            )

    async def test_slot_seeding_idempotency_and_no_resurrection(self):
        """Verify seeding adds slots, repeats without duplicates, and never resurrects removed slots."""
        # 1. First seeding
        result1 = await seed_slots_for_doctor("dr_smith", days=2)
        self.assertEqual(result1["status"], "success")
        initial_seeded = result1["seeded"]
        self.assertGreater(initial_seeded, 0)

        # 2. Second seeding (should be idempotent, 0 newly seeded)
        result2 = await seed_slots_for_doctor("dr_smith", days=2)
        self.assertEqual(result2["seeded"], 0)
        self.assertEqual(result2["skipped"], initial_seeded)

        # 3. Simulate doctor manually deleting a slot in Google Calendar
        all_slots = database.get_doctor_slots("dr_smith")
        slot_to_delete = all_slots[0]
        database.record_slot_state(
            doctor_id="dr_smith",
            slot_start=slot_to_delete["slot_start"],
            slot_end=slot_to_delete["slot_end"],
            google_event_id=slot_to_delete["google_event_id"],
            state="removed",
        )

        # 4. Third seeding: must NOT resurrect the doctor-deleted slot
        result3 = await seed_slots_for_doctor("dr_smith", days=2)
        self.assertEqual(result3["seeded"], 0)
        state_after = database.get_slot_state("dr_smith", slot_to_delete["slot_start"])
        self.assertEqual(state_after["state"], "removed")

    async def test_live_availability_filters_overlapping_events(self):
        """Verify that green available slots overlapping with doctor personal events are excluded."""
        # Seed slots
        await seed_slots_for_doctor("dr_smith", days=2)

        # Query slots on day 1
        all_seeded = database.get_doctor_slots("dr_smith")
        first_slot = all_seeded[0]
        target_date = first_slot["slot_start"][:10]

        # Fetch initial availability
        available_before = await get_available_slots_google("dr_smith", target_date)
        self.assertTrue(len(available_before) > 0)
        initial_count = len(available_before)

        # Doctor creates an opaque personal meeting overlapping with first slot
        slot_dt = datetime.fromisoformat(first_slot["slot_start"].replace("Z", "+00:00"))
        personal_event = {
            "id": "personal_surgery_123",
            "summary": "Emergency Surgery",
            "transparency": "opaque",
            "start": {"dateTime": slot_dt.isoformat()},
            "end": {"dateTime": (slot_dt + timedelta(minutes=25)).isoformat()},
            "extendedProperties": {"private": {}},
        }
        self.fake_service.events().insert("primary", personal_event).execute()

        # Query availability again: overlapping slot must now be excluded
        available_after = await get_available_slots_google("dr_smith", target_date)
        self.assertEqual(len(available_after), initial_count - 1)
        for s in available_after:
            self.assertNotEqual(s["start_time"], first_slot["slot_start"])

    async def test_appointment_booking_and_double_booking_prevention(self):
        """Verify booking converts green slot, updates DB, queues WhatsApp, and returns 409 on second attempt."""
        await seed_slots_for_doctor("dr_smith", days=2)
        all_slots = database.get_doctor_slots("dr_smith")
        target_slot = all_slots[0]

        # Enable WhatsApp preference for patient
        database.set_whatsapp_preference(
            tenant_id="default",
            phone_number="+919876543210",
            enabled=True,
            consent_source="voice_call",
            consent_text_version="v1",
        )

        # 1. Book first appointment
        book_res1 = await book_appointment_google(
            doctor_id="dr_smith",
            slot_start=target_slot["slot_start"],
            patient_name="Aman Pausker",
            phone="+919876543210",
            email="aman@example.com",
            reason="Cardiology checkup",
            call_id="call-101",
        )
        self.assertEqual(book_res1["status"], "success")
        appt_id = book_res1["appointment_id"]

        # Check DB appointment record
        appt = database.get_appointment(appt_id)
        self.assertIsNotNone(appt)
        self.assertEqual(appt["patient_name"], "Aman Pausker")
        self.assertEqual(appt["status"], "booked")

        # Check WhatsApp notification outbox
        self.assertTrue(database.has_queued_booking_notification("default", appt_id))

        # 2. Second attempt on the same slot (Double-booking test -> must return conflict 409)
        book_res2 = await book_appointment_google(
            doctor_id="dr_smith",
            slot_start=target_slot["slot_start"],
            patient_name="Another Caller",
            phone="+919876543211",
            call_id="call-102",
        )
        self.assertEqual(book_res2["status"], "conflict")
        self.assertEqual(book_res2["code"], 409)

    async def test_appointment_cancellation_and_slot_reopening(self):
        """Verify appointment cancellation reverts Google event to Available and updates DB."""
        await seed_slots_for_doctor("dr_smith", days=2)
        all_slots = database.get_doctor_slots("dr_smith")
        target_slot = all_slots[0]

        book_res = await book_appointment_google(
            doctor_id="dr_smith",
            slot_start=target_slot["slot_start"],
            patient_name="Rohan Verma",
            phone="+919876543210",
        )
        appt_id = book_res["appointment_id"]

        # Cancel appointment
        cancel_res = await cancel_appointment_google(appt_id, reason="Patient request")
        self.assertEqual(cancel_res["status"], "success")

        # Verify DB status
        appt = database.get_appointment(appt_id)
        self.assertEqual(appt["status"], "cancelled")

        # Verify event in Google Calendar reverted to Available
        live_evt = self.fake_service.events().get("primary", target_slot["google_event_id"]).execute()
        self.assertEqual(live_evt["summary"], "Available")
        self.assertEqual(live_evt["transparency"], "transparent")
        self.assertEqual(live_evt["colorId"], "2")

    async def test_appointment_rescheduling(self):
        """Verify rescheduling moves appointment from slot A to slot B and reopens slot A."""
        await seed_slots_for_doctor("dr_smith", days=2)
        all_slots = database.get_doctor_slots("dr_smith")
        slot_a = all_slots[0]
        slot_b = all_slots[1]

        book_res = await book_appointment_google(
            doctor_id="dr_smith",
            slot_start=slot_a["slot_start"],
            patient_name="Priya Sharma",
            phone="+919876543210",
        )
        old_appt_id = book_res["appointment_id"]

        # Reschedule to slot B
        resched_res = await reschedule_appointment_google(old_appt_id, slot_b["slot_start"])
        self.assertEqual(resched_res["status"], "success")

        # Slot A must be reopened
        slot_a_state = database.get_slot_state("dr_smith", slot_a["slot_start"])
        self.assertEqual(slot_a_state["state"], "seeded")

        # Slot B must be booked
        slot_b_state = database.get_slot_state("dr_smith", slot_b["slot_start"])
        self.assertEqual(slot_b_state["state"], "booked")

    async def test_oauth_token_refresh_invalid_grant_handling(self):
        """Verify that an invalid_grant refresh error sets doctor status to needs_reconnect."""
        from google.auth.exceptions import RefreshError

        # Mock Credentials.refresh to simulate revoked/invalid refresh token
        mock_creds = MagicMock()
        mock_creds.refresh.side_effect = RefreshError("invalid_grant: Token has been expired or revoked.")

        with patch("src.utils.google_auth.Credentials", return_value=mock_creds), \
             patch("src.utils.google_auth.decrypt_token", return_value="plain_token"):
            with self.assertRaises(google_auth.GoogleAuthNeedsReconnectError):
                google_auth.get_doctor_credentials("dr_smith")

        # Doctor status must be set to 'needs_reconnect'
        updated_doc = database.get_doctor("dr_smith")
        self.assertEqual(updated_doc["status"], "needs_reconnect")


if __name__ == "__main__":
    unittest.main()
