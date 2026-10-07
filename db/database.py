"""
SQLite Database utility for storing call transcripts, caller metadata, and appointment bookings.
"""

import sqlite3
import os
from datetime import datetime
from typing import Optional, Dict, Any, List

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_data.db")


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize database tables if they do not exist."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        
        # Calls table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS calls (
                call_id TEXT PRIMARY KEY,
                caller_number TEXT,
                status TEXT DEFAULT 'in-progress',
                transcript TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP
            );
        """)

        # Bookings table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT,
                cal_booking_id TEXT,
                guest_name TEXT,
                guest_email TEXT,
                start_time TEXT,
                notes TEXT,
                status TEXT DEFAULT 'confirmed',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (call_id) REFERENCES calls (call_id)
            );
        """)
        conn.commit()


def save_call_start(call_id: str, caller_number: Optional[str] = None) -> None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO calls (call_id, caller_number, status, started_at)
            VALUES (?, ?, 'in-progress', ?)
            """,
            (call_id, caller_number, datetime.utcnow().isoformat())
        )
        conn.commit()


def save_call_end(call_id: str, transcript: str, summary: str = "", status: str = "completed") -> None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE calls
            SET transcript = ?, summary = ?, status = ?, ended_at = ?
            WHERE call_id = ?
            """,
            (transcript, summary, status, datetime.utcnow().isoformat(), call_id)
        )
        conn.commit()


def save_booking(
    call_id: str,
    cal_booking_id: str,
    guest_name: str,
    guest_email: str,
    start_time: str,
    notes: str = ""
) -> None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO bookings (call_id, cal_booking_id, guest_name, guest_email, start_time, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (call_id, cal_booking_id, guest_name, guest_email, start_time, notes)
        )
        conn.commit()


def get_call(call_id: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


# Auto-initialize tables when imported
init_db()
