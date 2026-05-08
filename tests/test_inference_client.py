import pytest

from token_compare.inference_client import (
    discover_models, get_client_for_model, ModelInfo,
)


def test_discover_models_reads_three_addons(monkeypatch):
    monkeypatch.setenv("INFERENCE_URL", "https://us.inference.heroku.com")
    monkeypatch.setenv("INFERENCE_KEY", "inf-sonnet")
    monkeypatch.setenv("INFERENCE_MODEL_ID", "claude-4-5-sonnet")
    monkeypatch.setenv("HEROKU_INFERENCE_TEAL_URL", "https://us.inference.heroku.com")
    monkeypatch.setenv("HEROKU_INFERENCE_TEAL_KEY", "inf-haiku")
    monkeypatch.setenv("HEROKU_INFERENCE_TEAL_MODEL_ID", "claude-4-5-haiku")
    monkeypatch.setenv("HEROKU_INFERENCE_COBALT_URL", "https://us.inference.heroku.com")
    monkeypatch.setenv("HEROKU_INFERENCE_COBALT_KEY", "inf-opus")
    monkeypatch.setenv("HEROKU_INFERENCE_COBALT_MODEL_ID", "claude-opus-4-5")
    models = discover_models()
    ids = {m.model_id for m in models}
    assert ids == {"claude-4-5-haiku", "claude-4-5-sonnet", "claude-opus-4-5"}
    for m in models:
        assert m.url.startswith("https://")
        assert m.api_key.startswith("inf-")


def test_get_client_for_model_returns_anthropic_client(monkeypatch):
    monkeypatch.setenv("INFERENCE_URL", "https://x")
    monkeypatch.setenv("INFERENCE_KEY", "k")
    monkeypatch.setenv("INFERENCE_MODEL_ID", "claude-4-5-sonnet")
    client = get_client_for_model("claude-4-5-sonnet")
    # anthropic.Anthropic exposes .messages
    assert hasattr(client, "messages")


def test_unknown_model_raises():
    with pytest.raises(ValueError, match="no Heroku Inference addon"):
        get_client_for_model("not-a-real-model")
