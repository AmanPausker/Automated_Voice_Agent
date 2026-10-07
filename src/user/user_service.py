"""
User and caller identity service.
Identifies incoming callers by phone number to provide personalized greetings and call history.
"""

from typing import Optional, Dict, Any
from db.database import get_db_connection


def get_caller_profile(phone_number: str) -> Dict[str, Any]:
    """
    Lookup past bookings or call history for a given phone number.
    """
    if not phone_number:
        return {"known_user": False}

    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Check if caller has previous bookings
        cursor.execute(
            """
            SELECT guest_name, guest_email, start_time, notes, status
            FROM bookings
            WHERE call_id IN (SELECT call_id FROM calls WHERE caller_number = ?)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (phone_number,)
        )
        row = cursor.fetchone()
        
        if row:
            return {
                "known_user": True,
                "name": row["guest_name"],
                "email": row["guest_email"],
                "last_booking": row["start_time"],
                "last_status": row["status"],
            }

    return {"known_user": False}
