import io
import pytest
from PIL import Image


def _payload():
    return {
        "model": "sonnet", "models": ["sonnet"],
        "started_at": "2026-05-01T00:00:00+00:00",
        "finished_at": "2026-05-01T00:00:01+00:00",
        "operator": "me", "org_name": "myorg", "tool_commit": "abc123",
        "runs_per_path": 3,
        "scenarios": [
            {"scenario_id": "s01",
             "native_runs": [{"path":"native","input_tokens":1,"output_tokens":1,
                               "cache_read_input_tokens":0,"total_cost_usd":0.62,
                               "num_turns":1,"duration_ms":100,"tool_calls":[],
                               "succeeded":True,"raw_json":{}}],
             "mcp_runs":    [{"path":"mcp","input_tokens":1,"output_tokens":1,
                               "cache_read_input_tokens":0,"total_cost_usd":0.93,
                               "num_turns":1,"duration_ms":100,"tool_calls":[],
                               "succeeded":True,"raw_json":{}}]},
        ],
    }


def test_render_og_card_returns_png_bytes():
    from token_compare.og_render import render_og_card
    png_bytes = render_og_card(_payload(), theme="light", palette="teal-coral")
    assert isinstance(png_bytes, bytes)
    assert len(png_bytes) > 1000
    img = Image.open(io.BytesIO(png_bytes))
    assert img.size == (1200, 630)
    assert img.format == "PNG"


def test_render_og_card_dark_mode_changes_pixels():
    from token_compare.og_render import render_og_card
    light = render_og_card(_payload(), theme="light", palette="teal-coral")
    dark = render_og_card(_payload(), theme="dark", palette="teal-coral")
    assert light != dark


def test_render_og_card_palette_changes_pixels():
    from token_compare.og_render import render_og_card
    teal = render_og_card(_payload(), theme="light", palette="teal-coral")
    cyan = render_og_card(_payload(), theme="light", palette="cyan-amber")
    assert teal != cyan


def test_render_og_card_invalid_palette_falls_back():
    from token_compare.og_render import render_og_card
    out = render_og_card(_payload(), theme="light", palette="not-a-palette")
    assert isinstance(out, bytes)
    assert len(out) > 1000


def test_render_og_card_with_no_scenarios_renders_placeholder():
    from token_compare.og_render import render_og_card
    payload = {**_payload(), "scenarios": []}
    out = render_og_card(payload, theme="light", palette="teal-coral")
    img = Image.open(io.BytesIO(out))
    assert img.size == (1200, 630)
