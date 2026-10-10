"""
SQLite Database utility for storing call transcripts, caller metadata, and appointment bookings.
"""

import sqlite3
import os
import json
from contextlib import contextmanager
from datetime import datetime
from typing import Optional, Dict, Any, Iterator

DB_PATH = os.path.join(os.path.dirname(__file__), "agent_data.db")


@contextmanager
def get_db_connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


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
                phone_number TEXT,
                start_time TEXT,
                notes TEXT,
                status TEXT DEFAULT 'confirmed',
                tenant_id TEXT NOT NULL DEFAULT 'default',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (call_id) REFERENCES calls (call_id)
            );
        """)

        # Additive migrations preserve existing local SQLite databases.
        booking_columns = {
            row["name"] for row in cursor.execute("PRAGMA table_info(bookings)")
        }
        if "phone_number" not in booking_columns:
            cursor.execute("ALTER TABLE bookings ADD COLUMN phone_number TEXT")
        if "tenant_id" not in booking_columns:
            cursor.execute(
                "ALTER TABLE bookings ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'"
            )

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS whatsapp_preferences (
                tenant_id TEXT NOT NULL,
                phone_number TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                consent_at TIMESTAMP,
                revoked_at TIMESTAMP,
                consent_source TEXT NOT NULL,
                consent_text_version TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, phone_number)
            );
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notification_outbox (
                notification_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                appointment_id TEXT NOT NULL,
                patient_id TEXT,
                event_type TEXT NOT NULL CHECK (
                    event_type IN ('BOOKED', 'RESCHEDULED', 'CANCELLED')
                ),
                recipient_phone TEXT NOT NULL,
                template_name TEXT NOT NULL,
                template_language TEXT NOT NULL,
                template_parameters TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'QUEUED' CHECK (
                    delivery_status IN (
                        'QUEUED', 'PROCESSING', 'SENT', 'DELIVERED',
                        'READ', 'FAILED'
                    )
                ),
                retry_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                claimed_at TIMESTAMP,
                provider_message_id TEXT UNIQUE,
                error_details TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (tenant_id, appointment_id, event_type)
            );
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_notification_queue
            ON notification_outbox (delivery_status, next_attempt_at);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_notification_appointment
            ON notification_outbox (tenant_id, appointment_id);
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
    notes: str = "",
    phone_number: Optional[str] = None,
    tenant_id: str = "default",
    notification_template: Optional[Dict[str, Any]] = None,
) -> None:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO bookings (
                call_id, cal_booking_id, guest_name, guest_email,
                phone_number, start_time, notes, tenant_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id, cal_booking_id, guest_name, guest_email,
                phone_number, start_time, notes, tenant_id,
            ),
        )
        preference = None
        if phone_number:
            preference = cursor.execute(
                """
                SELECT enabled FROM whatsapp_preferences
                WHERE tenant_id = ? AND phone_number = ?
                """,
                (tenant_id, phone_number),
            ).fetchone()
        if (
            preference
            and preference["enabled"]
            and notification_template
            and cal_booking_id
        ):
            cursor.execute(
                """
                INSERT OR IGNORE INTO notification_outbox (
                    notification_id, tenant_id, appointment_id, patient_id,
                    event_type, recipient_phone, template_name,
                    template_language, template_parameters
                )
                VALUES (?, ?, ?, ?, 'BOOKED', ?, ?, ?, ?)
                """,
                (
                    f"{tenant_id}:BOOKED:{cal_booking_id}",
                    tenant_id,
                    cal_booking_id,
                    call_id,
                    phone_number,
                    notification_template["name"],
                    notification_template["language"],
                    json.dumps(notification_template["parameters"]),
                ),
            )
        conn.commit()


def set_whatsapp_preference(
    tenant_id: str,
    phone_number: str,
    enabled: bool,
    consent_source: str,
    consent_text_version: str,
) -> None:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_preferences (
                tenant_id, phone_number, enabled, consent_at, revoked_at,
                consent_source, consent_text_version, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant_id, phone_number) DO UPDATE SET
                enabled = excluded.enabled,
                consent_at = CASE
                    WHEN excluded.enabled = 1 THEN excluded.consent_at
                    ELSE whatsapp_preferences.consent_at
                END,
                revoked_at = CASE
                    WHEN excluded.enabled = 0 THEN excluded.revoked_at
                    ELSE NULL
                END,
                consent_source = excluded.consent_source,
                consent_text_version = excluded.consent_text_version,
                updated_at = excluded.updated_at
            """,
            (
                tenant_id,
                phone_number,
                int(enabled),
                now if enabled else None,
                None if enabled else now,
                consent_source,
                consent_text_version,
                now,
            ),
        )
        conn.commit()


def get_whatsapp_preference(tenant_id: str, phone_number: str) -> bool:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT enabled FROM whatsapp_preferences
            WHERE tenant_id = ? AND phone_number = ?
            """,
            (tenant_id, phone_number),
        ).fetchone()
        return bool(row and row["enabled"])


def has_queued_booking_notification(tenant_id: str, appointment_id: str) -> bool:
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM notification_outbox
            WHERE tenant_id = ? AND appointment_id = ? AND event_type = 'BOOKED'
            """,
            (tenant_id, appointment_id),
        ).fetchone()
        return row is not None


def claim_notification_batch(limit: int = 10) -> list[Dict[str, Any]]:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with get_db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE notification_outbox
            SET delivery_status = 'QUEUED', claimed_at = NULL,
                updated_at = ?
            WHERE delivery_status = 'PROCESSING'
              AND claimed_at < datetime('now', '-5 minutes')
            """,
            (now,),
        )
        rows = conn.execute(
            """
            SELECT notification_id FROM notification_outbox
            WHERE delivery_status = 'QUEUED' AND next_attempt_at <= ?
            ORDER BY created_at LIMIT ?
            """,
            (now, limit),
        ).fetchall()
        notification_ids = [row["notification_id"] for row in rows]
        if notification_ids:
            placeholders = ",".join("?" for _ in notification_ids)
            conn.execute(
                f"""
                UPDATE notification_outbox
                SET delivery_status = 'PROCESSING', claimed_at = ?,
                    updated_at = ?
                WHERE notification_id IN ({placeholders})
                """,
                (now, now, *notification_ids),
            )
            claimed = conn.execute(
                f"""
                SELECT * FROM notification_outbox
                WHERE notification_id IN ({placeholders})
                """,
                notification_ids,
            ).fetchall()
        else:
            claimed = []
        conn.commit()
        return [dict(row) for row in claimed]


def mark_notification_sent(notification_id: str, provider_message_id: str) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE notification_outbox
            SET delivery_status = 'SENT', provider_message_id = ?,
                claimed_at = NULL, error_details = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE notification_id = ? AND delivery_status = 'PROCESSING'
            """,
            (provider_message_id, notification_id),
        )
        conn.commit()


def mark_notification_retry(
    notification_id: str,
    error_details: str,
    next_attempt_at: Optional[str],
) -> None:
    status = "QUEUED" if next_attempt_at else "FAILED"
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE notification_outbox
            SET delivery_status = ?, retry_count = retry_count + 1,
                next_attempt_at = COALESCE(?, next_attempt_at),
                claimed_at = NULL, error_details = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE notification_id = ? AND delivery_status = 'PROCESSING'
            """,
            (status, next_attempt_at, error_details, notification_id),
        )
        conn.commit()


def update_notification_status(
    provider_message_id: str,
    delivery_status: str,
    error_details: Optional[str] = None,
) -> bool:
    allowed_statuses = {"SENT": 1, "DELIVERED": 2, "READ": 3, "FAILED": 4}
    if delivery_status not in allowed_statuses:
        return False
    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT notification_id, delivery_status
            FROM notification_outbox WHERE provider_message_id = ?
            """,
            (provider_message_id,),
        ).fetchone()
        if not row:
            return False
        current_status = row["delivery_status"]
        if current_status in {"FAILED", "READ"}:
            return True
        if (
            delivery_status != "FAILED"
            and current_status in allowed_statuses
            and allowed_statuses[delivery_status] < allowed_statuses[current_status]
        ):
            return True
        conn.execute(
            """
            UPDATE notification_outbox
            SET delivery_status = ?, error_details = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE notification_id = ?
            """,
            (delivery_status, error_details, row["notification_id"]),
        )
        conn.commit()
        return True


def get_call(call_id: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


# Auto-initialize tables when imported
init_db()
