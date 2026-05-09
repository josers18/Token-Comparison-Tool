import os
import pytest


def setup_module(module):
    os.environ.setdefault("SESSION_SECRET", "test-secret-share")


def test_issue_and_verify_round_trip():
    from token_compare.share_token import issue, verify
    token, expires = issue("rpt_abc", ttl_days=30)
    assert verify(token) == "rpt_abc"


def test_token_expired_raises():
    from token_compare.share_token import issue, verify, ShareTokenError
    import time
    token, _ = issue("rpt_abc", ttl_days=0)
    time.sleep(0.01)
    with pytest.raises(ShareTokenError):
        verify(token)


def test_tampered_token_raises():
    from token_compare.share_token import issue, verify, ShareTokenError
    token, _ = issue("rpt_abc", ttl_days=30)
    parts = token.split(".")
    assert len(parts) == 3
    sig = parts[2]
    tampered_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
    tampered = ".".join([parts[0], parts[1], tampered_sig])
    with pytest.raises(ShareTokenError):
        verify(tampered)


def test_malformed_token_raises():
    from token_compare.share_token import verify, ShareTokenError
    with pytest.raises(ShareTokenError):
        verify("not-a-real-token")
    with pytest.raises(ShareTokenError):
        verify("only.two")
    with pytest.raises(ShareTokenError):
        verify("")


def test_secret_rotation_invalidates(monkeypatch):
    from token_compare.share_token import issue, verify, ShareTokenError
    monkeypatch.setenv("SESSION_SECRET", "key-A")
    token, _ = issue("rpt_abc", ttl_days=30)
    monkeypatch.setenv("SESSION_SECRET", "key-B")
    with pytest.raises(ShareTokenError):
        verify(token)
