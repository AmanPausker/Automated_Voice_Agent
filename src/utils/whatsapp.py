"""Meta WhatsApp Cloud API delivery and notification queue processing."""

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv
from loguru import logger

from db.database import (
    claim_notification_batch,
    mark_notification_retry,
    mark_notification_sent,
    update_notification_status,
)

load_dotenv()

WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
WHATSAPP_BUSINESS_ACCOUNT_ID = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "").strip()
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "").strip()
WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "").strip()
WHATSAPP_TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en_US").strip()
WHATSAPP_BOOKING_TEMPLATE = os.getenv("WHATSAPP_BOOKING_TEMPLATE", "").strip()
WHATSAPP_TENANT_ID = os.getenv("WHATSAPP_TENANT_ID", "default").strip() or "default"
WHATSAPP_TIMEZONE = os.getenv("WHATSAPP_TIMEZONE", "Asia/Kolkata").strip()

MAX_RETRIES = 8
E164_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")
API_VERSION_PATTERN = re.compile(r"^v[0-9]+\.[0-9]+$")


def validate_whatsapp_configuration() -> list[str]:
    """Return non-secret configuration errors; an unconfigured service stays disabled."""
    issues = []
    if bool(WHATSAPP_ACCESS_TOKEN) != bool(WHATSAPP_PHONE_NUMBER_ID):
        issues.append(
            "WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID must be configured together"
        )
    if WHATSAPP_ACCESS_TOKEN and not API_VERSION_PATTERN.fullmatch(WHATSAPP_API_VERSION):
        issues.append("WHATSAPP_API_VERSION must use the format vNN.N")
    if WHATSAPP_ACCESS_TOKEN and not WHATSAPP_BOOKING_TEMPLATE:
        issues.append("WHATSAPP_BOOKING_TEMPLATE must name an approved booking template")
    if WHATSAPP_ACCESS_TOKEN and not WHATSAPP_TEMPLATE_LANGUAGE:
        issues.append("WHATSAPP_TEMPLATE_LANGUAGE is required when WhatsApp is enabled")
    if WHATSAPP_ACCESS_TOKEN and not WHATSAPP_BUSINESS_ACCOUNT_ID:
        issues.append("WHATSAPP_BUSINESS_ACCOUNT_ID is required when WhatsApp is enabled")
    if WHATSAPP_ACCESS_TOKEN and not WHATSAPP_VERIFY_TOKEN:
        issues.append("WHATSAPP_VERIFY_TOKEN is required when WhatsApp is enabled")
    if WHATSAPP_ACCESS_TOKEN and not WHATSAPP_APP_SECRET:
        issues.append("WHATSAPP_APP_SECRET is required when WhatsApp is enabled")
    if WHATSAPP_ACCESS_TOKEN:
        required_branding = (
            "WHATSAPP_HOSPITAL_NAME",
            "WHATSAPP_DOCTOR_NAME",
            "WHATSAPP_SPECIALIZATION",
            "WHATSAPP_HOSPITAL_ADDRESS",
            "WHATSAPP_HOSPITAL_PHONE",
        )
        missing_branding = [
            name for name in required_branding if not os.getenv(name, "").strip()
        ]
        if missing_branding:
            issues.append(
                "Required WhatsApp template branding is missing: "
                + ", ".join(missing_branding)
            )
    if bool(WHATSAPP_VERIFY_TOKEN) != bool(WHATSAPP_APP_SECRET):
        issues.append(
            "WHATSAPP_VERIFY_TOKEN and WHATSAPP_APP_SECRET must be configured together"
        )
    try:
        ZoneInfo(WHATSAPP_TIMEZONE)
    except ZoneInfoNotFoundError:
        issues.append("WHATSAPP_TIMEZONE is not a recognized IANA timezone")
    return issues


def log_whatsapp_configuration() -> None:
    issues = validate_whatsapp_configuration()
    for issue in issues:
        logger.error("WhatsApp configuration issue: {}", issue)
    if not WHATSAPP_ACCESS_TOKEN and not WHATSAPP_PHONE_NUMBER_ID:
        logger.warning("WhatsApp delivery is disabled; queued notifications will be retained")


def is_valid_e164_phone(phone_number: str) -> bool:
    return bool(E164_PATTERN.fullmatch(phone_number.strip()))


def build_booking_template(
    patient_name: str,
    start_time: str,
    appointment_id: str,
) -> Dict[str, Any]:
    """Snapshot approved-template parameters at booking time without sensitive notes."""
    try:
        instant = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        local_time = instant.astimezone(ZoneInfo(WHATSAPP_TIMEZONE))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Appointment time or WhatsApp timezone is invalid") from exc

    values = [
        patient_name,
        os.getenv("WHATSAPP_HOSPITAL_NAME", "").strip(),
        os.getenv("WHATSAPP_DOCTOR_NAME", "").strip(),
        os.getenv("WHATSAPP_SPECIALIZATION", "").strip(),
        local_time.strftime("%d %b %Y"),
        local_time.strftime("%I:%M %p"),
        appointment_id,
        os.getenv("WHATSAPP_HOSPITAL_ADDRESS", "").strip(),
        os.getenv("WHATSAPP_HOSPITAL_PHONE", "").strip(),
    ]
    if any(not value for value in values):
        raise ValueError("Required WhatsApp template values are not configured")
    if any(len(value) > 1024 for value in values):
        raise ValueError("WhatsApp template values must not exceed 1024 characters")
    return {
        "name": WHATSAPP_BOOKING_TEMPLATE,
        "language": WHATSAPP_TEMPLATE_LANGUAGE,
        "parameters": [
            {"type": "body", "parameters": [{"type": "text", "text": value} for value in values]}
        ],
    }


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    if not WHATSAPP_APP_SECRET or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        WHATSAPP_APP_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _provider_error(payload: Any) -> str:
    """Return a concise provider error identifier without retaining response PII."""
    try:
        error = payload.get("error", {})
        code = error.get("code", "unknown")
        subcode = error.get("error_subcode")
        return f"Meta API error code={code}" + (
            f" subcode={subcode}" if subcode is not None else ""
        )
    except (AttributeError, TypeError):
        return "Meta API returned an invalid error response"


def process_delivery_webhook(payload: Dict[str, Any]) -> int:
    """Apply recognized Meta delivery updates idempotently to known messages."""
    status_map = {
        "sent": "SENT",
        "delivered": "DELIVERED",
        "read": "READ",
        "failed": "FAILED",
    }
    updated = 0
    entries = payload.get("entry", [])
    if not isinstance(entries, list):
        return 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes", [])
        if not isinstance(changes, list):
            continue
        for change in changes:
            value = change.get("value", {}) if isinstance(change, dict) else {}
            if not isinstance(value, dict):
                continue
            statuses = value.get("statuses", [])
            if not isinstance(statuses, list):
                continue
            for status in statuses:
                if not isinstance(status, dict):
                    continue
                mapped_status = status_map.get(status.get("status"))
                provider_id = status.get("id")
                if not mapped_status or not isinstance(provider_id, str):
                    continue
                errors = status.get("errors") or []
                error_code = (
                    errors[0].get("code")
                    if isinstance(errors, list)
                    and errors
                    and isinstance(errors[0], dict)
                    else None
                )
                error_details = (
                    f"WhatsApp delivery error code={error_code}"
                    if error_code is not None
                    else None
                )
                if update_notification_status(provider_id, mapped_status, error_details):
                    updated += 1
    return updated


async def process_notification_queue(batch_size: int = 10) -> int:
    """Send queued events; API acceptance is recorded as SENT, not DELIVERED."""
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        return 0
    if validate_whatsapp_configuration():
        return 0

    claimed = claim_notification_batch(batch_size)
    for notification in claimed:
        notification_id = notification["notification_id"]
        if notification["event_type"] != "BOOKED":
            mark_notification_retry(
                notification_id, "No configured template for notification event", None
            )
            continue

        try:
            components = json.loads(notification["template_parameters"])
        except (TypeError, json.JSONDecodeError):
            mark_notification_retry(
                notification_id, "Stored WhatsApp template parameters are invalid", None
            )
            logger.error(
                "WhatsApp notification {} has invalid stored template data",
                notification_id,
            )
            continue
        payload = {
            "messaging_product": "whatsapp",
            "to": notification["recipient_phone"].lstrip("+"),
            "type": "template",
            "template": {
                "name": notification["template_name"] or WHATSAPP_BOOKING_TEMPLATE,
                "language": {
                    "code": notification["template_language"] or WHATSAPP_TEMPLATE_LANGUAGE
                },
                "components": components,
            },
        }
        url = (
            f"https://graph.facebook.com/{WHATSAPP_API_VERSION}/"
            f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
        )
        retry_count = notification["retry_count"]
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"},
                    json=payload,
                )
            try:
                response_data = response.json()
            except ValueError:
                response_data = {}
            if response.status_code in (200, 201):
                messages = (
                    response_data.get("messages", [])
                    if isinstance(response_data, dict)
                    else []
                )
                provider_id = (
                    messages[0].get("id")
                    if messages and isinstance(messages[0], dict)
                    else None
                )
                if not provider_id:
                    mark_notification_retry(
                        notification_id,
                        "Meta API accepted request but returned no message identifier",
                        None,
                    )
                    logger.error(
                        "WhatsApp accepted notification {} without returning a message ID",
                        notification_id,
                    )
                    continue
                mark_notification_sent(notification_id, provider_id)
                logger.info("WhatsApp notification {} accepted by Meta", notification_id)
                continue

            error_details = _provider_error(response_data)
            retryable = (
                response.status_code in (408, 429) or response.status_code >= 500
            )
        except httpx.RequestError as exc:
            error_details = f"WhatsApp network error: {type(exc).__name__}"
            retryable = True

        next_attempt = None
        if retryable and retry_count + 1 < MAX_RETRIES:
            delay = min(3600, 30 * (2 ** retry_count))
            next_attempt = (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).strftime("%Y-%m-%d %H:%M:%S")
        mark_notification_retry(notification_id, error_details, next_attempt)
        if next_attempt:
            logger.warning(
                "WhatsApp notification {} failed and was queued for retry",
                notification_id,
            )
        else:
            logger.error(
                "WhatsApp notification {} permanently failed: {}",
                notification_id,
                error_details,
            )
    return len(claimed)
