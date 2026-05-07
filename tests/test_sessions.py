import os
import pytest
from itsdangerous import BadSignature
from token_compare.sessions import sign_session_id, verify_session_id


def test_round_trip(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    signed = sign_session_id("abc123")
    assert verify_session_id(signed) == "abc123"


def test_tampered_cookie_rejected(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "x" * 64)
    signed = sign_session_id("abc123")
    bad = signed[:-2] + ("AA" if not signed.endswith("AA") else "BB")
    with pytest.raises(BadSignature):
        verify_session_id(bad)


def test_missing_secret_raises(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        sign_session_id("abc123")
