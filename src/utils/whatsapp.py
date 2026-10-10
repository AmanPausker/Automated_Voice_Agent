"""
WhatsApp notification utility using Meta WhatsApp Business Cloud API.
Sends booking confirmation messages to callers after appointment scheduling.
"""

import os
import httpx
from loguru import logger
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

WHATSAPP_API_VERSION = "v20.0"
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_TEMPLATE_NAME = os.getenv("WHATSAPP_TEMPLATE_NAME", "hello_world")
WHATSAPP_TEMPLATE_LANGUAGE = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en_US")
# Set to False when using simple templates like hello_world (no variables)
WHATSAPP_TEMPLATE_HAS_VARIABLES = os.getenv("WHATSAPP_TEMPLATE_HAS_VARIABLES", "false").lower() == "true"


def _format_phone_for_whatsapp(phone: str) -> str:
    """
    Ensure the phone number is in E.164 format without the leading '+'.
    Meta WA API expects: '919876543210' (no + sign).
    """
    phone = phone.strip()
    if phone.startswith("+"):
        phone = phone[1:]
    # Remove any spaces or dashes
    phone = phone.replace(" ", "").replace("-", "")
    return phone


def _format_datetime_readable(iso_time: str) -> str:
    """
    Convert ISO timestamp like '2026-10-09T10:00:00Z' to '9 Oct 2026, 10:00 AM UTC'.
    """
    try:
        dt = datetime.strptime(iso_time, "%Y-%m-%dT%H:%M:%SZ")
        return dt.strftime("%-d %b %Y, %-I:%M %p UTC")
    except Exception:
        return iso_time


async def send_booking_confirmation_whatsapp(
    to_phone: str,
    guest_name: str,
    start_time: str,
    booking_id: str = "",
) -> bool:
    """
    Send a WhatsApp booking confirmation message to the caller.

    Uses a Meta-approved message template. The template must be pre-created
    in your Meta Business Manager with the name defined in WHATSAPP_TEMPLATE_NAME.

    Template variable order (configure in Meta):
      {{1}} = guest name
      {{2}} = appointment time
      {{3}} = booking ID (optional)

    Args:
        to_phone:     Caller's phone number (E.164 format, e.g. '+919876543210')
        guest_name:   Name used during booking
        start_time:   ISO timestamp of the booked slot
        booking_id:   Cal.com booking ID for reference

    Returns:
        True if the message was sent successfully, False otherwise.
    """
    if not WHATSAPP_PHONE_NUMBER_ID or not WHATSAPP_ACCESS_TOKEN:
        logger.warning(
            "WhatsApp credentials not set. "
            "Add WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN to your .env"
        )
        return False

    to_number = _format_phone_for_whatsapp(to_phone)
    if not to_number:
        logger.warning(f"Invalid phone number for WhatsApp: {to_phone!r}")
        return False

    readable_time = _format_datetime_readable(start_time)
    ref = booking_id if booking_id else "N/A"

    url = (
        f"https://graph.facebook.com/{WHATSAPP_API_VERSION}"
        f"/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    # Template component parameters — must match your approved template's variable order
    template_payload: dict = {
        "name": WHATSAPP_TEMPLATE_NAME,
        "language": {"code": WHATSAPP_TEMPLATE_LANGUAGE},
    }

    # Only add variable components for parameterized templates.
    # Simple templates like hello_world have no variables and will reject components.
    if WHATSAPP_TEMPLATE_HAS_VARIABLES:
        template_payload["components"] = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": guest_name},       # {{1}} Name
                    {"type": "text", "text": readable_time},    # {{2}} Time
                    {"type": "text", "text": ref},              # {{3}} Booking ID
                ],
            }
        ]

    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "template",
        "template": template_payload,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.post(url, headers=headers, json=payload)

        if res.status_code == 200:
            logger.info(
                f"WhatsApp booking confirmation sent to {to_phone} "
                f"(booking: {booking_id})"
            )
            return True
        else:
            logger.error(
                f"WhatsApp API error [{res.status_code}]: {res.text}"
            )
            return False

    except httpx.RequestError as exc:
        logger.error(f"WhatsApp request failed: {exc}")
        return False
