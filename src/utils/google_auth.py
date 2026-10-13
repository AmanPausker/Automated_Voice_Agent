"""
Google OAuth 2.0 and token encryption utility for per-doctor Google Calendar access.
"""

import os
import json
import base64
import hashlib
from typing import Optional, Dict, Any, Tuple
from loguru import logger
from dotenv import load_dotenv
from cryptography.fernet import Fernet, InvalidToken
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import Flow

from db.database import get_doctor, upsert_doctor, set_doctor_status, log_audit_event

load_dotenv()

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_OAUTH_REDIRECT_URI = os.getenv(
    "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/oauth/google/callback"
).strip()
GOOGLE_TOKEN_ENCRYPTION_KEY = os.getenv("GOOGLE_TOKEN_ENCRYPTION_KEY", "").strip()

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
]

_DEFAULT_DEV_KEY = b"x_tXF0eM73kK1p9y_R7pLw9M6_zT8qA9v4sB1cD2eF4="


class GoogleAuthError(Exception):
    """Base error for Google OAuth operations."""
    pass


class GoogleAuthNeedsReconnectError(GoogleAuthError):
    """Raised when refresh token is revoked or expired and needs user reconnection."""
    pass


def _get_fernet() -> Fernet:
    """Return a Fernet cipher instance based on GOOGLE_TOKEN_ENCRYPTION_KEY or a secure fallback."""
    raw_key = GOOGLE_TOKEN_ENCRYPTION_KEY
    if raw_key:
        try:
            # If standard Fernet key (32 url-safe base64-encoded bytes)
            return Fernet(raw_key.encode("utf-8"))
        except Exception:
            # Derive deterministic 32-byte key from string
            digest = hashlib.sha256(raw_key.encode("utf-8")).digest()
            url_safe_key = base64.urlsafe_b64encode(digest)
            return Fernet(url_safe_key)

    logger.warning(
        "GOOGLE_TOKEN_ENCRYPTION_KEY is not set. Using fallback development key. "
        "Set a 32-byte base64 Fernet key in production."
    )
    return Fernet(_DEFAULT_DEV_KEY)


def encrypt_token(token: str) -> str:
    """Encrypt a sensitive token string using Fernet."""
    if not token:
        return ""
    fernet = _get_fernet()
    return fernet.encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_token(encrypted_token: str) -> str:
    """Decrypt an encrypted token string."""
    if not encrypted_token:
        return ""
    fernet = _get_fernet()
    try:
        return fernet.decrypt(encrypted_token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        logger.error("Failed to decrypt Google token: invalid key or corrupted payload")
        raise GoogleAuthError("Token decryption failed") from exc


def get_oauth_flow(redirect_uri: Optional[str] = None) -> Flow:
    """Create a Google OAuth Flow object."""
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri or GOOGLE_OAUTH_REDIRECT_URI],
        }
    }
    flow = Flow.from_client_config(
        client_config=client_config,
        scopes=SCOPES,
        redirect_uri=redirect_uri or GOOGLE_OAUTH_REDIRECT_URI,
    )
    return flow


def get_authorization_url(
    doctor_id: str,
    redirect_uri: Optional[str] = None,
    state_extra: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Generate Google OAuth consent URL for a doctor.
    Returns (authorization_url, state).
    """
    flow = get_oauth_flow(redirect_uri=redirect_uri)
    state_payload = {"doctor_id": doctor_id}
    if state_extra:
        state_payload["extra"] = state_extra
    state_str = base64.urlsafe_b64encode(json.dumps(state_payload).encode()).decode()

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
        state=state_str,
    )
    return auth_url, state_str


def decode_oauth_state(state_str: str) -> Dict[str, Any]:
    """Decode state returned in OAuth callback."""
    try:
        payload = base64.urlsafe_b64decode(state_str.encode()).decode()
        return json.loads(payload)
    except Exception:
        return {}


def exchange_code_for_tokens(
    code: str,
    redirect_uri: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Exchange authorization code for access and refresh tokens.
    """
    flow = get_oauth_flow(redirect_uri=redirect_uri)
    flow.fetch_token(code=code)
    credentials = flow.credentials
    return {
        "access_token": credentials.token,
        "refresh_token": credentials.refresh_token,
        "token_uri": credentials.token_uri,
        "client_id": credentials.client_id,
        "client_secret": credentials.client_secret,
        "scopes": credentials.scopes,
    }


def get_doctor_credentials(doctor_id: str) -> Optional[Credentials]:
    """
    Build authenticated Google Credentials for a doctor using their encrypted refresh token.
    Refreshes the access token if expired.
    Sets doctor status to 'needs_reconnect' if refresh fails with invalid_grant.
    """
    doctor = get_doctor(doctor_id)
    if not doctor:
        logger.warning(f"Doctor {doctor_id} not found in database")
        return None

    encrypted_refresh_token = doctor.get("encrypted_refresh_token")
    if not encrypted_refresh_token:
        logger.warning(f"Doctor {doctor_id} has no stored refresh token")
        return None

    try:
        refresh_token = decrypt_token(encrypted_refresh_token)
    except GoogleAuthError:
        set_doctor_status(doctor_id, "needs_reconnect")
        return None

    client_id = GOOGLE_CLIENT_ID
    client_secret = GOOGLE_CLIENT_SECRET

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )

    try:
        req = Request()
        creds.refresh(req)
        # If doctor was previously marked as needs_reconnect, set back to active
        if doctor.get("status") == "needs_reconnect":
            set_doctor_status(doctor_id, "active")
        return creds
    except RefreshError as exc:
        logger.error(f"Failed to refresh Google token for doctor {doctor_id}: {exc}")
        set_doctor_status(doctor_id, "needs_reconnect")
        log_audit_event(
            event_type="OAUTH_REFRESH_FAILURE",
            doctor_id=doctor_id,
            details={"error": str(exc)},
        )
        raise GoogleAuthNeedsReconnectError(
            f"Google authorization expired or revoked for doctor {doctor_id}. Reconnection required."
        ) from exc
    except Exception as exc:
        logger.exception(f"Unexpected error refreshing credentials for doctor {doctor_id}: {exc}")
        return None
