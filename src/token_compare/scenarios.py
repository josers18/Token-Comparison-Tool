from __future__ import annotations

from pathlib import Path

import yaml

from token_compare.models import Scenario


def load_file(path: Path) -> Scenario:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Scenario.model_validate(data)


def load_all(directory: Path) -> list[Scenario]:
    scenarios = sorted(
        (load_file(p) for p in Path(directory).glob("*.yaml")),
        key=lambda s: s.id,
    )
    seen: set[str] = set()
    for s in scenarios:
        if s.id in seen:
            raise ValueError(f"duplicate scenario id: {s.id}")
        seen.add(s.id)
    return scenarios
