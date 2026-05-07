from __future__ import annotations

# Per-1M-token USD prices for each Heroku Inference model.
# Sourced from Anthropic's published pricing for the equivalent Claude models.
# Update if Heroku Inference publishes its own pricing or Anthropic changes theirs.
# Keys are the model_id strings the Heroku Inference addons set as
# INFERENCE_MODEL_ID / HEROKU_INFERENCE_TEAL_MODEL_ID / HEROKU_INFERENCE_COBALT_MODEL_ID.
MODEL_PRICES: dict[str, dict[str, float]] = {
    "claude-4-5-haiku": {
        "input": 1.00,
        "output": 5.00,
        "cache_read": 0.10,
        "cache_creation": 1.25,
    },
    "claude-4-5-sonnet": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_creation": 3.75,
    },
    "claude-opus-4-5": {
        "input": 15.00,
        "output": 75.00,
        "cache_read": 1.50,
        "cache_creation": 18.75,
    },
}


def compute_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int,
    cache_creation_input_tokens: int,
) -> float:
    p = MODEL_PRICES.get(model)
    if not p:
        return 0.0
    per_million = 1_000_000.0
    return (
        input_tokens * p["input"] / per_million
        + output_tokens * p["output"] / per_million
        + cache_read_input_tokens * p["cache_read"] / per_million
        + cache_creation_input_tokens * p["cache_creation"] / per_million
    )
