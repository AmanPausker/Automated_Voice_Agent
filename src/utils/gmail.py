"""
Gmail / SMTP Notification Service for Automated Voice Agent.
Sends rich HTML appointment confirmation emails to patients and doctor notifications.
"""

import os
import smtplib
import asyncio
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import Dict, Any, Optional
from loguru import logger
from dotenv import load_dotenv

import httpx

load_dotenv()

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
GMAIL_USER = os.getenv("GMAIL_USER", "").strip()
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").strip()
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587").strip() or "587")
EMAIL_FROM_NAME = os.getenv("EMAIL_FROM_NAME", "Aman Pausker Clinic").strip()
HOSPITAL_NAME = os.getenv("WHATSAPP_HOSPITAL_NAME", "Aman Pausker Healthcare").strip()
DOCTOR_NAME = os.getenv("WHATSAPP_DOCTOR_NAME", "Dr. Aman Pausker").strip()
SPECIALIZATION = os.getenv("WHATSAPP_SPECIALIZATION", "General & Preventative Medicine").strip()
HOSPITAL_ADDRESS = os.getenv("WHATSAPP_HOSPITAL_ADDRESS", "101 Wellness Boulevard, Mumbai, MH").strip()
HOSPITAL_PHONE = os.getenv("WHATSAPP_HOSPITAL_PHONE", "+91 98765 43210").strip()


def is_gmail_configured() -> bool:
    """Check if either Resend API or Gmail SMTP credentials are configured in .env."""
    return bool(RESEND_API_KEY or (GMAIL_USER and GMAIL_APP_PASSWORD))


def format_slot_time(time_str: str) -> str:
    """Format an ISO time string into human-friendly 12-hour format."""
    try:
        clean = time_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        return dt.strftime("%A, %B %d, %Y at %I:%M %p")
    except Exception:
        return time_str


def _build_html_email(
    guest_name: str,
    start_time: str,
    booking_id: str,
    notes: str = "",
) -> str:
    formatted_time = format_slot_time(start_time)
    notes_html = f"<p><strong>Patient Notes:</strong> {notes}</p>" if notes else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f3f4f6; margin: 0; padding: 20px; color: #1f2937; }}
  .container {{ max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); }}
  .header {{ background: linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%); padding: 32px 24px; text-align: center; color: white; }}
  .header h1 {{ margin: 0 0 8px 0; font-size: 24px; font-weight: 700; }}
  .header p {{ margin: 0; opacity: 0.9; font-size: 15px; }}
  .content {{ padding: 32px 24px; }}
  .badge {{ display: inline-block; background: #d1fae5; color: #065f46; font-size: 12px; font-weight: 600; padding: 4px 12px; border-radius: 9999px; text-transform: uppercase; margin-bottom: 16px; }}
  .card {{ background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 8px; padding: 20px; margin: 20px 0; }}
  .detail-row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px dashed #e5e7eb; font-size: 14px; }}
  .detail-row:last-child {{ border-bottom: none; }}
  .detail-label {{ color: #6b7280; font-weight: 500; }}
  .detail-value {{ color: #111827; font-weight: 600; text-align: right; }}
  .footer {{ background: #f9fafb; padding: 20px 24px; font-size: 13px; color: #6b7280; text-align: center; border-top: 1px solid #e5e7eb; }}
  .btn {{ display: inline-block; background: #4f46e5; color: white; padding: 12px 24px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 14px; margin-top: 16px; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>{HOSPITAL_NAME}</h1>
    <p>Appointment Confirmation</p>
  </div>
  <div class="content">
    <div class="badge">Confirmed Booking</div>
    <p>Dear <strong>{guest_name}</strong>,</p>
    <p>Your appointment has been successfully scheduled. Here are the details of your upcoming consultation:</p>
    
    <div class="card">
      <div class="detail-row">
        <span class="detail-label">Physician</span>
        <span class="detail-value">{DOCTOR_NAME} ({SPECIALIZATION})</span>
      </div>
      <div class="detail-row">
        <span class="detail-label">Date & Time</span>
        <span class="detail-value">{formatted_time}</span>
      </div>
      <div class="detail-row">
        <span class="detail-label">Booking Reference</span>
        <span class="detail-value">{booking_id}</span>
      </div>
      <div class="detail-row">
        <span class="detail-label">Clinic Address</span>
        <span class="detail-value">{HOSPITAL_ADDRESS}</span>
      </div>
      <div class="detail-row">
        <span class="detail-label">Clinic Contact</span>
        <span class="detail-value">{HOSPITAL_PHONE}</span>
      </div>
    </div>

    {notes_html}

    <p style="font-size: 14px; color: #4b5563; line-height: 1.5;">
      <strong>Important Instructions:</strong><br>
      &bull; Please arrive 10 minutes prior to your scheduled time.<br>
      &bull; Keep this confirmation email handy upon arrival.<br>
      &bull; If you need to reschedule, please call {HOSPITAL_PHONE}.
    </p>
  </div>
  <div class="footer">
    <p>&copy; {datetime.now().year} {HOSPITAL_NAME}. All rights reserved.</p>
    <p>Automated confirmation sent via AI Voice Agent & Cal.com Scheduling.</p>
  </div>
</div>
</body>
</html>
"""


def _send_smtp_sync(to_email: str, subject: str, html_body: str, text_body: str) -> Dict[str, Any]:
    """Synchronous SMTP worker run inside asyncio thread pool."""
    if not is_gmail_configured():
        missing = []
        if not GMAIL_USER:
            missing.append("GMAIL_USER")
        if not GMAIL_APP_PASSWORD:
            missing.append("GMAIL_APP_PASSWORD")
        missing_str = ", ".join(missing)
        logger.warning(
            f"Gmail SMTP credentials incomplete (missing {missing_str}). "
            f"Email to {to_email} cannot be delivered without GMAIL_APP_PASSWORD."
        )
        return {
            "success": False,
            "simulated": True,
            "message": f"Email NOT sent to inbox: Missing {missing_str} in .env. Real email delivery requires a Google App Password.",
        }

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{EMAIL_FROM_NAME} <{GMAIL_USER}>"
    msg["To"] = to_email

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=12)
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, [to_email], msg.as_string())
        server.quit()
        logger.info(f"Successfully sent confirmation email to {to_email}")
        return {"success": True, "simulated": False, "message": f"Email delivered to {to_email}"}
    except Exception as exc:
        logger.error(f"Failed to send email to {to_email}: {exc}")
        return {"success": False, "simulated": False, "error": str(exc)}


async def _send_via_resend(to_email: str, subject: str, html_body: str, text_body: str) -> Dict[str, Any]:
    """Send appointment confirmation email via Resend API."""
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "from": f"{EMAIL_FROM_NAME} <onboarding@resend.dev>",
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code in (200, 201):
            data = resp.json()
            email_id = data.get("id", "delivered")
            logger.info(f"Successfully delivered email via Resend to {to_email} (ID: {email_id})")
            return {"success": True, "provider": "resend", "id": email_id, "message": f"Email delivered via Resend to {to_email}"}
        else:
            logger.error(f"Resend API error: {resp.status_code} - {resp.text}")
            return {"success": False, "provider": "resend", "error": resp.text}
    except Exception as exc:
        logger.error(f"Resend dispatch failed: {exc}")
        return {"success": False, "provider": "resend", "error": str(exc)}


async def send_booking_confirmation_email(
    guest_name: str,
    guest_email: str,
    start_time: str,
    booking_id: str,
    notes: str = "",
) -> Dict[str, Any]:
    """
    Sends an appointment confirmation email via Resend API or Gmail SMTP asynchronously.
    """
    if not guest_email or "@" not in guest_email:
        logger.warning(f"Invalid recipient email provided: {guest_email}")
        return {"success": False, "error": "Invalid email address"}

    subject = f"Appointment Confirmed: {DOCTOR_NAME} - {format_slot_time(start_time)}"
    html_content = _build_html_email(guest_name, start_time, booking_id, notes)
    plain_text = (
        f"Dear {guest_name},\n\n"
        f"Your appointment with {DOCTOR_NAME} ({SPECIALIZATION}) has been confirmed for {format_slot_time(start_time)}.\n"
        f"Booking Reference: {booking_id}\n"
        f"Clinic Address: {HOSPITAL_ADDRESS}\n"
        f"Clinic Phone: {HOSPITAL_PHONE}\n\n"
        f"Thank you,\n{HOSPITAL_NAME}"
    )

    if RESEND_API_KEY:
        return await _send_via_resend(
            to_email=guest_email,
            subject=subject,
            html_body=html_content,
            text_body=plain_text,
        )

    return await asyncio.to_thread(
        _send_smtp_sync,
        to_email=guest_email,
        subject=subject,
        html_body=html_content,
        text_body=plain_text,
    )

