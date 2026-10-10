import hashlib
import hmac
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx

import db.database as database
import src.tasks.cal_booking as cal_booking
import src.utils.whatsapp as whatsapp


class FakeAsyncClient:
    def __init__(self, response):
        self.response = response
        self.request_url = None
        self.request_headers = None
        self.request_payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def post(self, url, headers=None, json=None):
        self.request_url = url
        self.request_headers = headers
        self.request_payload = json
        return self.response


class WhatsAppNotificationTests(unittest.IsolatedAsyncioTestCase):
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
            database, "DB_PATH", os.path.join(self.temp_dir.name, "test.db")
        )
        self.db_path_patch.start()
        database.init_db()
        with database.get_db_connection() as conn:
            conn.execute(
                "INSERT INTO calls (call_id, caller_number) VALUES (?, ?)",
                ("call-1", "+14155552671"),
            )
            conn.commit()

    def tearDown(self):
        self.db_path_patch.stop()
        self.branding_patch.stop()
        self.temp_dir.cleanup()

    def _enable_consent(self, phone="+14155552671", tenant="clinic-a"):
        database.set_whatsapp_preference(
            tenant_id=tenant,
            phone_number=phone,
            enabled=True,
            consent_source="voice_call",
            consent_text_version="whatsapp-consent-v1",
        )

    def _save_booking(self, appointment_id="cal-123", tenant="clinic-a"):
        self._enable_consent(tenant=tenant)
        database.save_booking(
            call_id="call-1",
            cal_booking_id=appointment_id,
            guest_name="Alex Patient",
            guest_email="patient@example.test",
            start_time="2026-10-10T10:00:00Z",
            phone_number="+14155552671",
            tenant_id=tenant,
            notification_template={
                "name": "booking_confirmation",
                "language": "en_US",
                "parameters": [{"type": "body", "parameters": []}],
            },
        )

    def _notifications(self):
        with database.get_db_connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM notification_outbox ORDER BY created_at"
                )
            ]

    async def test_successful_cal_booking_atomically_enqueues_after_confirmation(self):
        self._enable_consent()
        response = httpx.Response(
            201,
            json={"data": {"id": 987, "start": "2026-10-10T10:00:00Z"}},
        )
        fake_client = FakeAsyncClient(response)
        with patch.object(cal_booking.httpx, "AsyncClient", return_value=fake_client), patch.object(
            cal_booking, "WHATSAPP_TENANT_ID", "clinic-a"
        ):
            result = await cal_booking.book_appointment(
                start_time="2026-10-10T10:00:00Z",
                name="Alex Patient",
                email="patient@example.test",
                phone="+14155552671",
                call_id="call-1",
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["whatsapp_notification"], "queued")
        rows = self._notifications()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["appointment_id"], "987")
        self.assertEqual(rows[0]["delivery_status"], "QUEUED")
        self.assertEqual(rows[0]["event_type"], "BOOKED")

    async def test_unsuccessful_cal_booking_does_not_enqueue(self):
        fake_client = FakeAsyncClient(
            httpx.Response(409, text="slot unavailable")
        )
        with patch.object(cal_booking.httpx, "AsyncClient", return_value=fake_client):
            result = await cal_booking.book_appointment(
                start_time="2026-10-10T10:00:00Z",
                name="Alex Patient",
                email="patient@example.test",
                phone="+14155552671",
                call_id="call-1",
            )
        self.assertFalse(result["success"])
        self.assertEqual(self._notifications(), [])

    async def test_declined_or_missing_consent_does_not_enqueue(self):
        database.save_booking(
            call_id="call-1",
            cal_booking_id="cal-no-consent",
            guest_name="Alex Patient",
            guest_email="patient@example.test",
            start_time="2026-10-10T10:00:00Z",
            phone_number="+14155552671",
            tenant_id="clinic-a",
            notification_template={
                "name": "booking_confirmation",
                "language": "en_US",
                "parameters": [],
            },
        )
        self.assertEqual(self._notifications(), [])

        self._enable_consent()
        database.set_whatsapp_preference(
            "clinic-a",
            "+14155552671",
            False,
            "voice_call",
            "whatsapp-consent-v1",
        )
        self.assertFalse(database.get_whatsapp_preference("clinic-a", "+14155552671"))

    async def test_duplicate_event_is_deduplicated_and_tenant_scoped(self):
        self._save_booking()
        self._save_booking()
        self._enable_consent(tenant="clinic-b")
        database.save_booking(
            call_id="call-1",
            cal_booking_id="cal-123",
            guest_name="Other Tenant",
            guest_email="other@example.test",
            start_time="2026-10-10T10:00:00Z",
            phone_number="+14155552671",
            tenant_id="clinic-b",
            notification_template={
                "name": "booking_confirmation",
                "language": "en_US",
                "parameters": [],
            },
        )
        rows = self._notifications()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["tenant_id"] for row in rows}, {"clinic-a", "clinic-b"})
        self.assertEqual(
            database.get_whatsapp_preference("clinic-a", "+14155552671"), True
        )
        self.assertEqual(
            database.get_whatsapp_preference("clinic-c", "+14155552671"), False
        )

    async def test_api_acceptance_is_sent_until_delivery_webhook(self):
        self._save_booking()
        fake_client = FakeAsyncClient(
            httpx.Response(200, json={"messages": [{"id": "wamid.abc"}]})
        )
        config = patch.multiple(
            whatsapp,
            WHATSAPP_ACCESS_TOKEN="secret-test-token",
            WHATSAPP_PHONE_NUMBER_ID="phone-id",
            WHATSAPP_BUSINESS_ACCOUNT_ID="business-id",
            WHATSAPP_VERIFY_TOKEN="verify-token",
            WHATSAPP_APP_SECRET="app-secret",
            WHATSAPP_API_VERSION="v22.0",
            WHATSAPP_BOOKING_TEMPLATE="booking_confirmation",
            WHATSAPP_TEMPLATE_LANGUAGE="en_US",
        )
        with config, patch.object(whatsapp.httpx, "AsyncClient", return_value=fake_client):
            processed = await whatsapp.process_notification_queue()

        self.assertEqual(processed, 1)
        self.assertEqual(fake_client.request_payload["to"], "14155552671")
        self.assertEqual(
            fake_client.request_headers["Authorization"], "Bearer secret-test-token"
        )
        self.assertEqual(self._notifications()[0]["delivery_status"], "SENT")
        self.assertTrue(database.update_notification_status("wamid.abc", "DELIVERED"))
        self.assertEqual(self._notifications()[0]["delivery_status"], "DELIVERED")

    async def test_retryable_api_failure_is_requeued_and_permanent_error_is_logged(self):
        self._save_booking()
        fake_client = FakeAsyncClient(
            httpx.Response(429, json={"error": {"code": 4}})
        )
        config = patch.multiple(
            whatsapp,
            WHATSAPP_ACCESS_TOKEN="test-token",
            WHATSAPP_PHONE_NUMBER_ID="phone-id",
            WHATSAPP_BUSINESS_ACCOUNT_ID="business-id",
            WHATSAPP_VERIFY_TOKEN="verify-token",
            WHATSAPP_APP_SECRET="app-secret",
            WHATSAPP_API_VERSION="v22.0",
            WHATSAPP_BOOKING_TEMPLATE="booking_confirmation",
            WHATSAPP_TEMPLATE_LANGUAGE="en_US",
        )
        with config, patch.object(whatsapp.httpx, "AsyncClient", return_value=fake_client):
            await whatsapp.process_notification_queue()
        row = self._notifications()[0]
        self.assertEqual(row["delivery_status"], "QUEUED")
        self.assertEqual(row["retry_count"], 1)
        self.assertIn("code=4", row["error_details"])

        with database.get_db_connection() as conn:
            conn.execute(
                "UPDATE notification_outbox SET next_attempt_at = '2000-01-01 00:00:00'"
            )
            conn.commit()
        permanent_client = FakeAsyncClient(
            httpx.Response(400, json={"error": {"code": 132000}})
        )
        with config, patch.object(
            whatsapp.httpx, "AsyncClient", return_value=permanent_client
        ):
            await whatsapp.process_notification_queue()
        row = self._notifications()[0]
        self.assertEqual(row["delivery_status"], "FAILED")
        self.assertIn("code=132000", row["error_details"])

    async def test_missing_configuration_leaves_outbox_pending(self):
        self._save_booking()
        with patch.multiple(
            whatsapp,
            WHATSAPP_ACCESS_TOKEN="",
            WHATSAPP_PHONE_NUMBER_ID="",
        ):
            processed = await whatsapp.process_notification_queue()
        self.assertEqual(processed, 0)
        self.assertEqual(self._notifications()[0]["delivery_status"], "QUEUED")

    async def test_invalid_phone_and_webhook_signature(self):
        self.assertTrue(whatsapp.is_valid_e164_phone("+14155552671"))
        self.assertFalse(whatsapp.is_valid_e164_phone("4155552671"))
        self.assertFalse(whatsapp.is_valid_e164_phone("+0123456789"))
        with patch.object(whatsapp, "WHATSAPP_APP_SECRET", "test-app-secret"):
            raw_body = b'{"entry":[]}'
            signature = "sha256=" + hmac.new(
                b"test-app-secret", raw_body, hashlib.sha256
            ).hexdigest()
            self.assertTrue(whatsapp.verify_webhook_signature(raw_body, signature))
            self.assertFalse(whatsapp.verify_webhook_signature(raw_body, "sha256=wrong"))
        with patch.dict(os.environ, {"WHATSAPP_HOSPITAL_PHONE": ""}):
            with self.assertRaises(ValueError):
                whatsapp.build_booking_template(
                    "Alex", "2026-10-10T10:00:00Z", "cal-123"
                )

    async def test_delivery_webhook_is_idempotent_and_never_downgrades_status(self):
        self._save_booking()
        database.claim_notification_batch()
        database.mark_notification_sent("clinic-a:BOOKED:cal-123", "wamid.delivery")
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "statuses": [
                                    {"id": "wamid.delivery", "status": "delivered"}
                                ]
                            }
                        }
                    ]
                }
            ]
        }
        self.assertEqual(whatsapp.process_delivery_webhook(payload), 1)
        self.assertEqual(whatsapp.process_delivery_webhook(payload), 1)
        self.assertEqual(
            whatsapp.process_delivery_webhook(
                {
                    "entry": [
                        {
                            "changes": [
                                {
                                    "value": {
                                        "statuses": [
                                            {"id": "wamid.delivery", "status": "sent"}
                                        ]
                                    }
                                }
                            ]
                        }
                    ]
                }
            ),
            1,
        )
        self.assertEqual(self._notifications()[0]["delivery_status"], "DELIVERED")

    async def test_template_time_and_required_configuration_validation(self):
        with patch.object(whatsapp, "WHATSAPP_TIMEZONE", "not/a-timezone"):
            with self.assertRaises(ValueError):
                whatsapp.build_booking_template(
                    "Alex", "2026-10-10T10:00:00Z", "cal-123"
                )
        with patch.multiple(
            whatsapp,
            WHATSAPP_ACCESS_TOKEN="token",
            WHATSAPP_PHONE_NUMBER_ID="",
            WHATSAPP_BUSINESS_ACCOUNT_ID="",
            WHATSAPP_API_VERSION="",
            WHATSAPP_BOOKING_TEMPLATE="",
            WHATSAPP_VERIFY_TOKEN="verify-token",
            WHATSAPP_APP_SECRET="",
        ):
            issues = whatsapp.validate_whatsapp_configuration()
        self.assertTrue(any("configured together" in issue for issue in issues))
        self.assertTrue(any("API_VERSION" in issue for issue in issues))
        self.assertTrue(any("approved booking template" in issue for issue in issues))
        self.assertTrue(any("BUSINESS_ACCOUNT_ID" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main()
