from __future__ import annotations

import os
from dataclasses import dataclass

from anthropic import Anthropic


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    url: str
    api_key: str


# (env_url_key, env_key_key, env_model_key) tuples for each attached
# Heroku Inference addon. Order is the dropdown order in the UI:
# cheap → mid → premium.
_ADDONS = [
    ("HEROKU_INFERENCE_TEAL_URL",  "HEROKU_INFERENCE_TEAL_KEY",  "HEROKU_INFERENCE_TEAL_MODEL_ID"),
    ("INFERENCE_URL",              "INFERENCE_KEY",              "INFERENCE_MODEL_ID"),
    ("HEROKU_INFERENCE_COBALT_URL","HEROKU_INFERENCE_COBALT_KEY","HEROKU_INFERENCE_COBALT_MODEL_ID"),
]


def discover_models() -> list[ModelInfo]:
    out: list[ModelInfo] = []
    for url_k, key_k, model_k in _ADDONS:
        url = os.environ.get(url_k)
        key = os.environ.get(key_k)
        model = os.environ.get(model_k)
        if url and key and model:
            out.append(ModelInfo(model_id=model, url=url, api_key=key))
    return out


def get_client_for_model(model_id: str) -> Anthropic:
    for m in discover_models():
        if m.model_id == model_id:
            return Anthropic(base_url=m.url, api_key=m.api_key)
    raise ValueError(f"no Heroku Inference addon for model_id={model_id!r}")
