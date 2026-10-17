"""
Test suite for doctor-facing CRUD operations.

Covers:
  - Call lifecycle  (CREATE call start, READ call, UPDATE call end)
  - Booking CRUD    (CREATE booking, READ via caller profile, tenant isolation)
  - WhatsApp prefs  (CREATE/UPDATE preference, READ status)
  - Notification    (CREATE outbox entry, claim/mark sent/retry/fail lifecycle)
  - get_caller_profile from user_service
  - Edge-cases: missing call_id, unknown phone number, empty fields
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

import db.database as database
from src.user.user_service import get_caller_profile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_call(conn, call_id="call-doc-1", caller="+911234567890"):
    conn.execute(
        "INSERT INTO calls (call_id, caller_number) VALUES (?, ?)",
        (call_id, caller),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Base test class – isolates every test in a fresh in-memory DB
# ---------------------------------------------------------------------------

class DoctorCRUDBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self._db_patch = unittest.mock.patch.object(
            database, "DB_PATH", os.path.join(self.temp_dir.name, "test_crud.db")
        )
        self._db_patch.start()
        database.init_db()

    def tearDown(self):
        self._db_patch.stop()
        self.temp_dir.cleanup()

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------
    def _seed_call(self, call_id="call-doc-1", caller="+911234567890"):
        with database.get_db_connection() as conn:
            _make_call(conn, call_id=call_id, caller=caller)

    def _seed_booking(
        self,
        call_id="call-doc-1",
        booking_id="booking-001",
        guest_name="Dr Patient",
        guest_email="patient@clinic.test",
        start_time="2026-11-01T09:00:00Z",
        phone="+911234567890",
        tenant_id="clinic-doctor",
        notification_template=None,
    ):
        database.save_booking(
            call_id=call_id,
            cal_booking_id=booking_id,
            guest_name=guest_name,
            guest_email=guest_email,
            start_time=start_time,
            phone_number=phone,
            tenant_id=tenant_id,
            notification_template=notification_template,
        )

    def _enable_whatsapp(self, phone="+911234567890", tenant="clinic-doctor"):
        database.set_whatsapp_preference(
            tenant_id=tenant,
            phone_number=phone,
            enabled=True,
            consent_source="voice_call",
            consent_text_version="v1",
        )

    def _all_bookings(self):
        with database.get_db_connection() as conn:
            return [
                dict(r)
                for r in conn.execute("SELECT * FROM bookings ORDER BY created_at").fetchall()
            ]

    def _all_notifications(self):
        with database.get_db_connection() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM notification_outbox ORDER BY created_at"
                ).fetchall()
            ]


# ===========================================================================
# 1. CALL LIFECYCLE
# ===========================================================================

class TestCallCRUD(DoctorCRUDBase):
    """CREATE / READ / UPDATE for the `calls` table."""

    def test_create_call_start(self):
        """save_call_start inserts a new call row."""
        database.save_call_start("call-new", "+910000000001")
        row = database.get_call("call-new")
        self.assertIsNotNone(row)
        self.assertEqual(row["call_id"], "call-new")
        self.assertEqual(row["caller_number"], "+910000000001")
        self.assertEqual(row["status"], "in-progress")
        self.assertIsNone(row["ended_at"])

    def test_read_nonexistent_call_returns_none(self):
        """get_call returns None when the call_id does not exist."""
        self.assertIsNone(database.get_call("does-not-exist"))

    def test_update_call_end(self):
        """save_call_end updates transcript, summary, status, and ended_at."""
        database.save_call_start("call-end-test", "+910000000002")
        database.save_call_end(
            call_id="call-end-test",
            transcript="Patient asked about 9 AM slot.",
            summary="Booking inquiry",
            status="completed",
        )
        row = database.get_call("call-end-test")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["transcript"], "Patient asked about 9 AM slot.")
        self.assertEqual(row["summary"], "Booking inquiry")
        self.assertIsNotNone(row["ended_at"])

    def test_update_call_end_custom_status(self):
        """save_call_end can set status to non-default value like 'abandoned'."""
        database.save_call_start("call-aban", "+910000000003")
        database.save_call_end("call-aban", "", status="abandoned")
        self.assertEqual(database.get_call("call-aban")["status"], "abandoned")

    def test_save_call_start_upsert_replaces_existing(self):
        """Calling save_call_start twice with the same call_id replaces the row."""
        database.save_call_start("call-dup", "+910000000004")
        database.save_call_start("call-dup", "+910000000005")
        row = database.get_call("call-dup")
        self.assertEqual(row["caller_number"], "+910000000005")

    def test_multiple_calls_are_independent(self):
        """Multiple calls do not interfere with each other."""
        database.save_call_start("call-A", "+910000000006")
        database.save_call_start("call-B", "+910000000007")
        database.save_call_end("call-A", "transcript A", status="completed")

        row_a = database.get_call("call-A")
        row_b = database.get_call("call-B")
        self.assertEqual(row_a["status"], "completed")
        self.assertEqual(row_b["status"], "in-progress")


# ===========================================================================
# 2. BOOKING CRUD
# ===========================================================================

class TestBookingCRUD(DoctorCRUDBase):
    """CREATE booking and READ via calls/bookings tables."""

    def setUp(self):
        super().setUp()
        self._seed_call()

    def test_create_booking_persists_all_fields(self):
        """save_booking stores all fields correctly."""
        self._seed_booking()
        rows = self._all_bookings()
        self.assertEqual(len(rows), 1)
        b = rows[0]
        self.assertEqual(b["cal_booking_id"], "booking-001")
        self.assertEqual(b["guest_name"], "Dr Patient")
        self.assertEqual(b["guest_email"], "patient@clinic.test")
        self.assertEqual(b["phone_number"], "+911234567890")
        self.assertEqual(b["start_time"], "2026-11-01T09:00:00Z")
        self.assertEqual(b["tenant_id"], "clinic-doctor")
        self.assertEqual(b["status"], "confirmed")

    def test_create_booking_without_phone(self):
        """save_booking with phone=None stores a null phone_number."""
        database.save_booking(
            call_id="call-doc-1",
            cal_booking_id="no-phone-booking",
            guest_name="Anonymous",
            guest_email="anon@clinic.test",
            start_time="2026-11-01T10:00:00Z",
            phone_number=None,
            tenant_id="clinic-doctor",
        )
        rows = self._all_bookings()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["phone_number"])

    def test_create_booking_without_notification_template_skips_outbox(self):
        """No outbox entry is created when notification_template is None."""
        self._seed_booking(notification_template=None)
        self.assertEqual(self._all_notifications(), [])

    def test_booking_without_consent_skips_outbox(self):
        """Notification is NOT queued when WhatsApp consent was not given."""
        # No call to _enable_whatsapp → no consent row
        self._seed_booking(
            notification_template={
                "name": "booking_confirmation",
                "language": "en_US",
                "parameters": [],
            }
        )
        self.assertEqual(self._all_notifications(), [])

    def test_booking_with_consent_enqueues_notification(self):
        """Notification IS queued when both consent and template are provided."""
        self._enable_whatsapp()
        self._seed_booking(
            notification_template={
                "name": "booking_confirmation",
                "language": "en_US",
                "parameters": [{"type": "body", "parameters": []}],
            }
        )
        rows = self._all_notifications()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delivery_status"], "QUEUED")
        self.assertEqual(rows[0]["event_type"], "BOOKED")
        self.assertEqual(rows[0]["appointment_id"], "booking-001")

    def test_duplicate_booking_same_tenant_is_deduplicated(self):
        """Saving the same booking twice for the same tenant only queues once."""
        self._enable_whatsapp()
        tpl = {
            "name": "booking_confirmation",
            "language": "en_US",
            "parameters": [],
        }
        self._seed_booking(notification_template=tpl)
        self._seed_booking(notification_template=tpl)  # duplicate
        self.assertEqual(len(self._all_notifications()), 1)

    def test_same_booking_id_different_tenant_creates_two_outbox_rows(self):
        """Same appointment_id but different tenant → separate outbox entries."""
        self._seed_call(call_id="call-tenant-b", caller="+911234567891")
        self._enable_whatsapp(tenant="clinic-doctor")
        self._enable_whatsapp(phone="+911234567891", tenant="clinic-b")

        tpl = {"name": "booking_confirmation", "language": "en_US", "parameters": []}
        self._seed_booking(notification_template=tpl, tenant_id="clinic-doctor")

        database.save_booking(
            call_id="call-tenant-b",
            cal_booking_id="booking-001",   # same ID, different tenant
            guest_name="Other Doc",
            guest_email="other@clinic.test",
            start_time="2026-11-01T09:00:00Z",
            phone_number="+911234567891",
            tenant_id="clinic-b",
            notification_template=tpl,
        )

        rows = self._all_notifications()
        self.assertEqual(len(rows), 2)
        tenant_ids = {r["tenant_id"] for r in rows}
        self.assertIn("clinic-doctor", tenant_ids)
        self.assertIn("clinic-b", tenant_ids)


# ===========================================================================
# 3. CALLER PROFILE (READ via user_service)
# ===========================================================================

class TestCallerProfile(DoctorCRUDBase):
    """get_caller_profile reads past bookings for a given phone number."""

    def setUp(self):
        super().setUp()
        self._seed_call()

    def test_unknown_caller_returns_known_false(self):
        """A phone with no history returns known_user=False."""
        result = get_caller_profile("+919999999999")
        self.assertFalse(result["known_user"])

    def test_empty_phone_returns_known_false(self):
        """An empty string returns known_user=False without hitting the DB."""
        result = get_caller_profile("")
        self.assertFalse(result["known_user"])

    def test_known_caller_returns_profile(self):
        """A phone with at least one booking returns known_user=True with details."""
        self._seed_booking()
        result = get_caller_profile("+911234567890")
        self.assertTrue(result["known_user"])
        self.assertEqual(result["name"], "Dr Patient")
        self.assertEqual(result["email"], "patient@clinic.test")
        self.assertEqual(result["last_booking"], "2026-11-01T09:00:00Z")
        self.assertEqual(result["last_status"], "confirmed")

    def test_most_recent_booking_is_returned(self):
        """With multiple bookings the latest one is surfaced."""
        self._seed_call(call_id="call-doc-2", caller="+911234567890")
        self._seed_booking(
            call_id="call-doc-1",
            booking_id="booking-old",
            guest_name="Dr Patient",
            guest_email="patient@clinic.test",
            start_time="2026-10-01T09:00:00Z",
        )
        self._seed_booking(
            call_id="call-doc-2",
            booking_id="booking-new",
            guest_name="Dr Patient Updated",
            guest_email="patient2@clinic.test",
            start_time="2026-11-15T11:00:00Z",
        )
        result = get_caller_profile("+911234567890")
        self.assertTrue(result["known_user"])
        # Should surface the most recent by created_at DESC
        self.assertEqual(result["last_booking"], "2026-11-15T11:00:00Z")


# ===========================================================================
# 4. WHATSAPP PREFERENCE CRUD
# ===========================================================================

class TestWhatsAppPreferenceCRUD(DoctorCRUDBase):
    """CREATE / UPDATE / READ for whatsapp_preferences table."""

    def test_create_preference_enabled(self):
        """set_whatsapp_preference persists an enabled preference."""
        self._enable_whatsapp()
        self.assertTrue(database.get_whatsapp_preference("clinic-doctor", "+911234567890"))

    def test_create_preference_disabled(self):
        """set_whatsapp_preference persists a disabled preference."""
        database.set_whatsapp_preference(
            "clinic-doctor", "+911234567890", False, "voice_call", "v1"
        )
        self.assertFalse(database.get_whatsapp_preference("clinic-doctor", "+911234567890"))

    def test_unknown_phone_returns_false(self):
        """get_whatsapp_preference returns False for unknown numbers."""
        self.assertFalse(database.get_whatsapp_preference("clinic-doctor", "+910000000000"))

    def test_update_preference_enable_then_revoke(self):
        """Revoking consent switches enabled → False."""
        self._enable_whatsapp()
        self.assertTrue(database.get_whatsapp_preference("clinic-doctor", "+911234567890"))

        database.set_whatsapp_preference(
            "clinic-doctor", "+911234567890", False, "voice_call", "v1"
        )
        self.assertFalse(database.get_whatsapp_preference("clinic-doctor", "+911234567890"))

    def test_tenant_isolation_for_preferences(self):
        """Preferences are scoped per tenant; enabling for clinic-A does not affect clinic-B."""
        self._enable_whatsapp(tenant="clinic-doctor")
        self.assertTrue(database.get_whatsapp_preference("clinic-doctor", "+911234567890"))
        self.assertFalse(database.get_whatsapp_preference("clinic-b", "+911234567890"))


# ===========================================================================
# 5. NOTIFICATION OUTBOX LIFECYCLE
# ===========================================================================

class TestNotificationOutboxLifecycle(DoctorCRUDBase):
    """Full lifecycle: QUEUE → PROCESSING → SENT → DELIVERED / FAILED."""

    def setUp(self):
        super().setUp()
        self._seed_call()
        self._enable_whatsapp()
        self._seed_booking(
            notification_template={
                "name": "booking_confirmation",
                "language": "en_US",
                "parameters": [{"type": "body", "parameters": []}],
            }
        )

    # ---- has_queued_booking_notification ----------------------------------

    def test_has_queued_booking_notification_true(self):
        """Returns True immediately after a booking with consent."""
        self.assertTrue(
            database.has_queued_booking_notification("clinic-doctor", "booking-001")
        )

    def test_has_queued_booking_notification_false_for_unknown(self):
        """Returns False for an appointment that was never saved."""
        self.assertFalse(
            database.has_queued_booking_notification("clinic-doctor", "nonexistent")
        )

    # ---- claim_notification_batch -----------------------------------------

    def test_claim_batch_transitions_to_processing(self):
        """Claimed notifications move from QUEUED → PROCESSING."""
        claimed = database.claim_notification_batch(limit=10)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["delivery_status"], "PROCESSING")
        # DB should also reflect the change
        row = self._all_notifications()[0]
        self.assertEqual(row["delivery_status"], "PROCESSING")

    def test_claim_batch_empty_when_nothing_queued(self):
        """claim_notification_batch returns [] when the queue is empty."""
        # Drain the queue first
        database.claim_notification_batch()
        second_batch = database.claim_notification_batch()
        self.assertEqual(second_batch, [])

    # ---- mark_notification_sent -------------------------------------------

    def test_mark_notification_sent(self):
        """After claiming, mark_notification_sent sets status to SENT."""
        database.claim_notification_batch()
        notification_id = self._all_notifications()[0]["notification_id"]
        database.mark_notification_sent(notification_id, "provider-msg-id-abc")

        row = self._all_notifications()[0]
        self.assertEqual(row["delivery_status"], "SENT")
        self.assertEqual(row["provider_message_id"], "provider-msg-id-abc")
        self.assertIsNone(row["claimed_at"])  # released after send

    # ---- update_notification_status (delivery webhook) --------------------

    def test_update_status_sent_to_delivered(self):
        """Delivery webhook can advance SENT → DELIVERED."""
        database.claim_notification_batch()
        notification_id = self._all_notifications()[0]["notification_id"]
        database.mark_notification_sent(notification_id, "wamid.doc1")

        result = database.update_notification_status("wamid.doc1", "DELIVERED")
        self.assertTrue(result)
        self.assertEqual(self._all_notifications()[0]["delivery_status"], "DELIVERED")

    def test_update_status_delivered_to_read(self):
        """Status can advance DELIVERED → READ."""
        database.claim_notification_batch()
        nid = self._all_notifications()[0]["notification_id"]
        database.mark_notification_sent(nid, "wamid.doc2")
        database.update_notification_status("wamid.doc2", "DELIVERED")
        database.update_notification_status("wamid.doc2", "READ")
        self.assertEqual(self._all_notifications()[0]["delivery_status"], "READ")

    def test_update_status_does_not_downgrade(self):
        """A DELIVERED notification cannot be downgraded back to SENT."""
        database.claim_notification_batch()
        nid = self._all_notifications()[0]["notification_id"]
        database.mark_notification_sent(nid, "wamid.doc3")
        database.update_notification_status("wamid.doc3", "DELIVERED")
        # Attempt to downgrade
        result = database.update_notification_status("wamid.doc3", "SENT")
        self.assertTrue(result)  # idempotent – returns True without downgrading
        self.assertEqual(self._all_notifications()[0]["delivery_status"], "DELIVERED")

    def test_update_status_unknown_provider_id_returns_false(self):
        """Returns False when provider_message_id is not found."""
        result = database.update_notification_status("wamid.unknown", "DELIVERED")
        self.assertFalse(result)

    def test_update_status_invalid_value_returns_false(self):
        """Returns False for an unrecognised delivery_status value."""
        result = database.update_notification_status("wamid.doc3", "UNKNOWN_STATUS")
        self.assertFalse(result)

    # ---- mark_notification_retry ------------------------------------------

    def test_mark_notification_retry_requeues(self):
        """After a retryable failure the notification is requeued with retry_count+1."""
        database.claim_notification_batch()
        nid = self._all_notifications()[0]["notification_id"]
        future = (datetime.utcnow() + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        database.mark_notification_retry(nid, "rate-limit error", future)

        row = self._all_notifications()[0]
        self.assertEqual(row["delivery_status"], "QUEUED")
        self.assertEqual(row["retry_count"], 1)
        self.assertIn("rate-limit", row["error_details"])

    def test_mark_notification_retry_with_none_next_attempt_marks_failed(self):
        """Passing next_attempt_at=None marks the notification FAILED."""
        database.claim_notification_batch()
        nid = self._all_notifications()[0]["notification_id"]
        database.mark_notification_retry(nid, "permanent error", None)

        row = self._all_notifications()[0]
        self.assertEqual(row["delivery_status"], "FAILED")

    def test_stale_processing_notifications_are_reclaimed(self):
        """Notifications stuck in PROCESSING for >5 min are reset to QUEUED on next claim."""
        database.claim_notification_batch()
        # Force claimed_at into the past
        with database.get_db_connection() as conn:
            conn.execute(
                "UPDATE notification_outbox SET claimed_at = '2000-01-01 00:00:00'"
            )
            conn.commit()

        second_batch = database.claim_notification_batch()
        self.assertEqual(len(second_batch), 1)
        self.assertEqual(second_batch[0]["delivery_status"], "PROCESSING")


# ---------------------------------------------------------------------------
# Import fix: patch.object is on unittest.mock
# ---------------------------------------------------------------------------
import unittest.mock  # noqa: E402  (needed for DoctorCRUDBase.setUp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
