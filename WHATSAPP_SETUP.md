# WhatsApp booking notifications

This repository currently has a single-tenant Cal.com booking flow. After Cal.com confirms a booking, the booking row and (only for a caller with recorded opt-in) its notification outbox row are written in one SQLite transaction. A background worker sends the approved Meta template asynchronously. API acceptance is recorded as `SENT`; only Meta delivery webhooks advance a notification to `DELIVERED` or `READ`. A WhatsApp outage does not roll back or delay a confirmed appointment.

## Meta Business setup

1. Create or select a Meta Business portfolio and WhatsApp Business Account, add a sender phone number, and complete Meta's business/phone verification as required.
2. In the Meta developer app, add the WhatsApp product and configure a production system-user access token with only the permissions needed to send messages and manage the WhatsApp account. Store it in the deployment secret manager as `WHATSAPP_ACCESS_TOKEN`; never put it in browser code or commit it.
3. Set `WHATSAPP_PHONE_NUMBER_ID` to the sender's phone-number ID (not its visible phone number), `WHATSAPP_BUSINESS_ACCOUNT_ID` to the WABA ID, and `WHATSAPP_API_VERSION` to a currently supported Graph API version (`vNN.N`).
4. Create and submit a utility template for approval in WhatsApp Manager. Configure its language and body placeholders in this exact order:
   1. Patient name
   2. Hospital/clinic name
   3. Doctor name
   4. Specialization
   5. Local appointment date
   6. Local appointment time
   7. Cal.com appointment ID
   8. Hospital/clinic address
   9. Hospital/clinic phone

   Example body: `Hello {{1}}, your appointment at {{2}} with {{3}} ({{4}}) is booked for {{5}} at {{6}}. Reference: {{7}}. Address: {{8}}. For assistance, contact {{9}}.`
   The template must be approved before use. Put its exact name in `WHATSAPP_BOOKING_TEMPLATE` and its approved locale (for example `en_US`) in `WHATSAPP_TEMPLATE_LANGUAGE`. Keep messages limited to appointment logistics; do not add diagnoses, symptoms, or medical notes.
5. Set hospital/doctor display values and an IANA timezone using `.env.example` as a variable-name reference. Provide secrets through environment variables or the deployment's secret manager. Never commit the real `.env`.

## Webhook and deployment

1. Expose `https://<public-host>/webhooks/whatsapp` over HTTPS.
2. In the Meta app's WhatsApp webhook configuration, subscribe to the `messages` field. Use a long random `WHATSAPP_VERIFY_TOKEN` for the verification challenge and set `WHATSAPP_APP_SECRET` to the Meta app secret. The POST webhook validates `X-Hub-Signature-256` against the exact request body before consuming delivery statuses.
3. Configure the sender phone number's webhook subscription and verify the callback URL. Test `sent`, `delivered`, `read`, and `failed` callbacks with Meta's test tools.
4. Deploy with persistent, backed-up storage for `db/agent_data.db`; SQLite queue state is local to that database. Run a single application process/replica for this SQLite-backed worker. A multi-replica production deployment should migrate the outbox to a shared transactional database with worker-safe claiming before scaling out.
5. Apply/review the additive schema changes by starting the app against a backup/staging database. The database initializes missing preference and outbox tables and adds missing booking columns automatically.
6. Monitor startup logs for configuration issues and inspect `notification_outbox` for `QUEUED`, retry counts, `SENT`, `DELIVERED`, `READ`, and permanent `FAILED` records. Failure details intentionally retain provider codes rather than raw provider payloads or patient data.

Without complete API credentials, appointment booking remains active and queued notifications are retained until configuration is corrected. Notifications are sent only after explicit voice-call opt-in for the caller-ID number; the number must be in E.164 form (`+` and country code). The voice agent also records opt-outs. Meta determines whether a syntactically valid number is registered on WhatsApp; rejected-recipient responses are recorded as permanent failures.

## Consent and current scope

Consent is stored per configured tenant and phone number, with enabled state, consent/revocation timestamps, source, and consent-text version. A phone number alone is not consent. Patients can enable or disable notification preference during a voice call by clearly agreeing or asking to stop. Do not add automatic opt-in based on booking or caller ID.

This codebase does not currently contain patient authentication/profile pages, a tenant registry, or reschedule/cancel appointment operations. Consequently this implementation deliberately adds no public preference/log-management endpoint and queues booking confirmations only. `WHATSAPP_TENANT_ID` and tenant branding are deployment-owned configuration, not client input; they support a single trusted tenant per deployment, not independent multi-hospital isolation. Do not use this as a multi-tenant deployment until identity, tenant authorization, per-tenant data isolation, and tenant-specific WhatsApp configuration have been implemented.

WhatsApp Cloud API delivery is asynchronous and at-least-once across a process crash after provider acceptance. The outbox's unique `(tenant_id, appointment_id, event_type)` key prevents duplicate logical booking events, while a network/worker crash at the provider-acceptance boundary cannot be made exactly-once without provider-supported idempotency.
