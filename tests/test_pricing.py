from token_compare.pricing import compute_cost_usd, MODEL_PRICES


def test_known_model_computes_cost():
    cost = compute_cost_usd(
        model="claude-4-5-sonnet",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    p = MODEL_PRICES["claude-4-5-sonnet"]
    expected = p["input"] + p["output"]
    assert abs(cost - expected) < 1e-9


def test_cache_tokens_priced_separately():
    cost = compute_cost_usd(
        model="claude-4-5-sonnet",
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
    )
    p = MODEL_PRICES["claude-4-5-sonnet"]
    expected = p["cache_read"] + p["cache_creation"]
    assert abs(cost - expected) < 1e-9


def test_unknown_model_returns_zero():
    cost = compute_cost_usd(
        model="some-unrecognized-model",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    assert cost == 0.0


def test_haiku_priced():
    assert "claude-4-5-haiku" in MODEL_PRICES
    p = MODEL_PRICES["claude-4-5-haiku"]
    assert p["input"] == 1.00
    assert p["output"] == 5.00
    assert p["cache_read"] == 0.10
    assert p["cache_creation"] == 1.25


def test_opus_priced():
    assert "claude-opus-4-5" in MODEL_PRICES
    p = MODEL_PRICES["claude-opus-4-5"]
    assert p["input"] == 15.00
    assert p["output"] == 75.00
    assert p["cache_read"] == 1.50
    assert p["cache_creation"] == 18.75
