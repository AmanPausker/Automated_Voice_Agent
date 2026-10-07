"""
Cal.com API integration for checking available slots and scheduling appointments.
"""

import os
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
import httpx
from dotenv import load_dotenv
from db.database import save_booking

load_dotenv()

CAL_API_KEY = (
    os.getenv("CAL.COM_API_KEY")
    or os.getenv("CAL_COM_API_KEY")
    or os.getenv("CALCOM_API_KEY")
    or ""
)

BASE_URL = "https://api.cal.com/v2"
DEFAULT_TIMEZONE = "Asia/Calcutta"
DEFAULT_EVENT_TYPE_ID = 7328617  # 15 min meeting


def _get_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {CAL_API_KEY.strip()}",
        "Content-Type": "application/json",
    }


async def list_event_types() -> List[Dict[str, Any]]:
    """Fetch available event types for the host."""
    async with httpx.AsyncClient() as client:
        res = await client.get(f"{BASE_URL}/event-types", headers=_get_headers())
        if res.status_code == 200:
            data = res.json().get("data", {})
            groups = data.get("eventTypeGroups", [])
            all_types = []
            for g in groups:
                all_types.extend(g.get("eventTypes", []))
            return all_types
        return []


async def get_available_slots(
    date_str: str,
    event_type_id: int = DEFAULT_EVENT_TYPE_ID,
    timezone: str = DEFAULT_TIMEZONE,
) -> List[str]:
    """
    Get available time slots for a specific date (YYYY-MM-DD).
    Returns list of human-readable slot times (e.g. ['10:00 AM', '10:30 AM', ...]).
    """
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        # Fallback if invalid format
        target_date = datetime.now().date() + timedelta(days=1)
        date_str = target_date.strftime("%Y-%m-%d")

    start_time = f"{date_str}T00:00:00Z"
    end_time = f"{date_str}T23:59:59Z"

    params = {
        "startTime": start_time,
        "endTime": end_time,
        "eventTypeId": event_type_id,
    }

    async with httpx.AsyncClient() as client:
        res = await client.get(
            f"{BASE_URL}/slots/available",
            headers=_get_headers(),
            params=params,
        )

        if res.status_code != 200:
            return []

        data = res.json()
        slots_by_day = data.get("data", {}).get("slots", {})
        slots = slots_by_day.get(date_str, [])

        # Format ISO slots to ISO strings or formatted times
        return [s.get("time") for s in slots if "time" in s]


async def book_appointment(
    start_time: str,
    name: str,
    email: str,
    phone: Optional[str] = None,
    notes: str = "",
    call_id: str = "direct",
    event_type_id: int = DEFAULT_EVENT_TYPE_ID,
    timezone: str = DEFAULT_TIMEZONE,
) -> Dict[str, Any]:
    """
    Book an appointment on Cal.com and record it in the database.
    """
    attendee_data: Dict[str, Any] = {
        "name": name,
        "email": email,
        "timeZone": timezone,
    }
    if phone:
        attendee_data["phoneNumber"] = phone

    payload = {
        "eventTypeId": event_type_id,
        "start": start_time,
        "attendee": attendee_data,
        "metadata": {
            "notes": notes,
            "call_id": call_id,
        },
    }

    async with httpx.AsyncClient() as client:
        res = await client.post(
            f"{BASE_URL}/bookings",
            headers=_get_headers(),
            json=payload,
        )

        if res.status_code in (200, 201):
            booking_data = res.json().get("data", {})
            booking_id = str(booking_data.get("id", ""))
            
            # Persist in local DB
            save_booking(
                call_id=call_id,
                cal_booking_id=booking_id,
                guest_name=name,
                guest_email=email,
                start_time=start_time,
                notes=notes,
            )

            return {
                "success": True,
                "booking_id": booking_id,
                "start": start_time,
                "message": f"Appointment booked successfully for {name} at {start_time}",
            }
        else:
            return {
                "success": False,
                "error": res.text,
                "status_code": res.status_code,
            }
