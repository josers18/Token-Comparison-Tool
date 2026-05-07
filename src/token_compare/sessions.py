from __future__ import annotations

import os
from itsdangerous import BadSignature, URLSafeSerializer

COOKIE_NAME = "tct_sid"


def _serializer() -> URLSafeSerializer:
    secret = os.environ.get("SESSION_SECRET")
    if not secret:
        raise RuntimeError("SESSION_SECRET is not set")
    return URLSafeSerializer(secret, salt="tct-session")


def sign_session_id(session_id: str) -> str:
    return _serializer().dumps(session_id)


def verify_session_id(signed: str) -> str:
    """Returns the unsigned session id. Raises BadSignature if tampered."""
    return _serializer().loads(signed)


__all__ = ["COOKIE_NAME", "sign_session_id", "verify_session_id", "BadSignature"]
