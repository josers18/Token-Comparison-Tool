from __future__ import annotations

import base64
import hmac
import hashlib
import os
from datetime import datetime, timedelta, timezone


class ShareTokenError(Exception):
    """Raised when a share token is malformed, tampered, or expired."""


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def _sign(report_id: str, expires_iso: str, secret: bytes) -> bytes:
    msg = f"{report_id}.{expires_iso}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).digest()


def _secret() -> bytes:
    s = os.environ.get("SESSION_SECRET")
    if not s:
        raise ShareTokenError("SESSION_SECRET not set")
    return s.encode("utf-8")


def issue(report_id: str, *, ttl_days: int = 30) -> tuple[str, datetime]:
    """Mint a share token. Returns (token, expires_at_utc)."""
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)
    iso = expires_at.isoformat(timespec="seconds")
    sig = _sign(report_id, iso, _secret())
    token = ".".join([
        _b64u_encode(report_id.encode("utf-8")),
        _b64u_encode(iso.encode("utf-8")),
        _b64u_encode(sig),
    ])
    return token, expires_at


def verify(token: str) -> str:
    """Returns the report_id if valid; raises ShareTokenError otherwise."""
    if not token or token.count(".") != 2:
        raise ShareTokenError("malformed")
    rid_b64, iso_b64, sig_b64 = token.split(".")
    try:
        report_id = _b64u_decode(rid_b64).decode("utf-8")
        expires_iso = _b64u_decode(iso_b64).decode("utf-8")
        provided_sig = _b64u_decode(sig_b64)
    except (ValueError, UnicodeDecodeError) as e:
        raise ShareTokenError(f"malformed: {e}") from e
    expected = _sign(report_id, expires_iso, _secret())
    if not hmac.compare_digest(provided_sig, expected):
        raise ShareTokenError("invalid signature")
    try:
        expires_at = datetime.fromisoformat(expires_iso)
    except ValueError as e:
        raise ShareTokenError(f"malformed expiry: {e}") from e
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= expires_at:
        raise ShareTokenError("expired")
    return report_id
