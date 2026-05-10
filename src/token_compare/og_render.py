"""Pillow-based OG card renderer.

Generates 1200×630 PNGs for Slack/Twitter/iMessage unfurls. No headless
browser; reads palette tokens from PALETTES below and composes directly.

Public:
    render_og_card(payload: dict, *, theme: str, palette: str) -> bytes
"""
from __future__ import annotations

import io
from pathlib import Path
from statistics import median
from typing import Literal

from PIL import Image, ImageDraw, ImageFont

# Mirrors the CSS tokens.css palette definitions. Keep in sync.
PALETTES = {
    ("light", "teal-coral"):        {"bg": (247, 247, 251),  "ink": (11, 15, 31), "ink_dim": (90, 97, 120),
                                      "native": (15, 118, 110),  "native2": (20, 184, 166),
                                      "mcp": (178, 58, 31),       "mcp2": (244, 114, 182)},
    ("dark",  "teal-coral"):        {"bg": (11, 15, 31),     "ink": (248, 248, 252), "ink_dim": (160, 160, 180),
                                      "native": (94, 234, 212),  "native2": (20, 184, 166),
                                      "mcp": (253, 164, 175),     "mcp2": (251, 113, 133)},
    ("light", "emerald-violet"):    {"bg": (247, 247, 251),  "ink": (11, 15, 31), "ink_dim": (90, 97, 120),
                                      "native": (4, 120, 87),    "native2": (16, 185, 129),
                                      "mcp": (109, 40, 217),      "mcp2": (167, 139, 250)},
    ("dark",  "emerald-violet"):    {"bg": (11, 15, 31),     "ink": (248, 248, 252), "ink_dim": (160, 160, 180),
                                      "native": (110, 231, 183), "native2": (16, 185, 129),
                                      "mcp": (196, 181, 253),     "mcp2": (139, 92, 246)},
    ("light", "cyan-amber"):        {"bg": (247, 247, 251),  "ink": (11, 15, 31), "ink_dim": (90, 97, 120),
                                      "native": (3, 105, 161),   "native2": (14, 165, 233),
                                      "mcp": (180, 83, 9),        "mcp2": (245, 158, 11)},
    ("dark",  "cyan-amber"):        {"bg": (11, 15, 31),     "ink": (248, 248, 252), "ink_dim": (160, 160, 180),
                                      "native": (125, 211, 252), "native2": (14, 165, 233),
                                      "mcp": (252, 211, 77),      "mcp2": (245, 158, 11)},
    ("light", "forest-terracotta"): {"bg": (247, 247, 251),  "ink": (11, 15, 31), "ink_dim": (90, 97, 120),
                                      "native": (20, 83, 45),    "native2": (21, 128, 61),
                                      "mcp": (154, 52, 18),       "mcp2": (194, 65, 12)},
    ("dark",  "forest-terracotta"): {"bg": (11, 15, 31),     "ink": (248, 248, 252), "ink_dim": (160, 160, 180),
                                      "native": (187, 247, 208), "native2": (34, 197, 94),
                                      "mcp": (253, 186, 116),     "mcp2": (234, 88, 12)},
}

DEFAULT_KEY = ("light", "teal-coral")

W, H = 1200, 630
PAD = 80

_FONT_DIR = Path(__file__).resolve().parents[2] / "static" / "fonts"


def _load_font(name: str, size: int) -> ImageFont.FreeTypeFont:
    path = _FONT_DIR / name
    return ImageFont.truetype(str(path), size=size)


def _gradient_rect(draw: ImageDraw.ImageDraw, box, c1, c2):
    x0, y0, x1, y1 = box
    width = x1 - x0
    if width <= 0:
        return
    for i in range(width):
        t = i / max(1, width - 1)
        r = int(c1[0] + (c2[0] - c1[0]) * t)
        g = int(c1[1] + (c2[1] - c1[1]) * t)
        b = int(c1[2] + (c2[2] - c1[2]) * t)
        draw.line([(x0 + i, y0), (x0 + i, y1)], fill=(r, g, b))


def _aggregate_costs(payload: dict) -> tuple[float | None, float | None, float | None]:
    """Return (native_med, mcp_med, multiplier) over all scenarios in the payload."""
    n_costs: list[float] = []
    m_costs: list[float] = []
    for sc in payload.get("scenarios", []) or []:
        for r in sc.get("native_runs", []) or []:
            if r.get("succeeded"):
                n_costs.append(float(r["total_cost_usd"]))
        for r in sc.get("mcp_runs", []) or []:
            if r.get("succeeded"):
                m_costs.append(float(r["total_cost_usd"]))
    n_med = float(median(n_costs)) if n_costs else None
    m_med = float(median(m_costs)) if m_costs else None
    mult = (m_med / n_med) if (n_med and m_med and n_med > 0) else None
    return n_med, m_med, mult


def render_og_card(
    payload: dict,
    *,
    theme: Literal["light", "dark"] = "light",
    palette: str = "teal-coral",
) -> bytes:
    key = (theme, palette)
    p = PALETTES.get(key, PALETTES[DEFAULT_KEY])

    img = Image.new("RGB", (W, H), p["bg"])
    draw = ImageDraw.Draw(img, mode="RGB")

    # Subtle gradient strip at the top — the palette identity.
    _gradient_rect(draw, (0, 0, W, 6), p["native"], p["mcp"])

    # Soft "glow" — a single color rectangle at low opacity overlaid by alpha-blend.
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    glow_color = (*p["native"], 38) if theme == "light" else (*p["native"], 50)
    gdraw.rectangle((W * 0.55, -100, W + 100, H * 0.7), fill=glow_color)
    img.paste(Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB"))

    # Eyebrow.
    f_eyebrow = _load_font("JetBrainsMono.ttf", 22)
    draw.text((PAD, PAD), "TOKENMETER · SHARED REPORT", fill=p["ink_dim"], font=f_eyebrow)

    # Headline (Fraunces).
    n_med, m_med, mult = _aggregate_costs(payload)
    if mult and mult > 1.05:
        line = "Native is"
        big = f"{mult:.1f}×"
        tail = "cheaper here."
        big_color = p["native"]
    elif mult and mult < 0.95:
        line = "MCP is"
        big = f"{1.0/mult:.1f}×"
        tail = "cheaper here."
        big_color = p["mcp"]
    elif mult is not None:
        line = "Costs are"
        big = "1.0×"
        tail = "essentially equal."
        big_color = p["ink"]
    else:
        line = "Tokenmeter"
        big = "report"
        tail = ""
        big_color = p["ink"]

    f_serif = _load_font("Fraunces.ttf", 96)
    f_big = _load_font("Fraunces.ttf", 132)

    y = PAD + 56
    draw.text((PAD, y), line, fill=p["ink"], font=f_serif)
    bbox = draw.textbbox((PAD, y), line, font=f_serif)
    big_x = bbox[2] + 24
    draw.text((big_x, y - 12), big, fill=big_color, font=f_big)
    big_bbox = draw.textbbox((big_x, y - 12), big, font=f_big)
    tail_x = big_bbox[2] + 24
    draw.text((tail_x, y), tail, fill=p["ink"], font=f_serif)

    # Two number cards along the bottom.
    f_card_label = _load_font("JetBrainsMono.ttf", 18)
    f_card_value = _load_font("JetBrainsMono.ttf", 56)

    card_y = H - PAD - 130
    card_w = (W - 2 * PAD - 24) // 2
    card_h = 130

    def card(x, label, value, accent):
        # rounded rect
        draw.rounded_rectangle((x, card_y, x + card_w, card_y + card_h),
                                radius=14, fill=p["bg"],
                                outline=accent, width=2)
        draw.text((x + 24, card_y + 18), label, fill=p["ink_dim"], font=f_card_label)
        draw.text((x + 24, card_y + 50), value, fill=accent, font=f_card_value)

    card(PAD, "NATIVE", f"${n_med:.4f}" if n_med is not None else "—", p["native"])
    card(PAD + card_w + 24, "MCP", f"${m_med:.4f}" if m_med is not None else "—", p["mcp"])

    # Footer: model + scenarios.
    f_foot = _load_font("JetBrainsMono.ttf", 16)
    n_scn = len(payload.get("scenarios", []) or [])
    model = (payload.get("model") or "—").upper()
    foot_text = f"{model}  ·  {n_scn} SCENARIOS  ·  {payload.get('operator','')}"
    draw.text((PAD, H - 40), foot_text, fill=p["ink_dim"], font=f_foot)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
