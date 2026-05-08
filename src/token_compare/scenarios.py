from __future__ import annotations

from pathlib import Path

import yaml

from token_compare.models import Scenario, SuccessCriteria


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


def _row_to_scenario(row: dict) -> Scenario:
    """Build a Scenario from a scenarios-table row."""
    sc = row.get("success_criteria_json") or {"must_contain": []}
    return Scenario(
        id=row["id"],
        title=row["title"],
        category=row["category"],
        difficulty=row["difficulty"],
        prompt=row["prompt"],
        expected_operations=row.get("expected_operations") or [],
        success_criteria=SuccessCriteria.model_validate(sc),
        notes=row.get("notes", "") or "",
    )


async def load_all_from_db() -> list[Scenario]:
    """Read the scenarios table and return only the active (non-soft-deleted)
    scenarios as Pydantic Scenario objects. Used by the API at request time
    so admin edits take effect immediately without a redeploy."""
    from token_compare import db
    rows = await db.list_scenarios(include_inactive=False)
    return [_row_to_scenario(r) for r in rows]


async def seed_from_yaml_if_empty(directory: Path) -> int:
    """If the scenarios table is empty, import every *.yaml under
    `directory` into it. Returns the number of rows inserted (0 if the
    table was already populated). Idempotent: safe to call on every
    dyno startup."""
    from token_compare import db
    if (await db.count_scenarios()) > 0:
        return 0
    inserted = 0
    for path in sorted(Path(directory).glob("*.yaml")):
        s = load_file(path)
        await db.upsert_scenario(
            id=s.id,
            title=s.title,
            category=s.category,
            difficulty=s.difficulty,
            prompt=s.prompt,
            expected_operations=list(s.expected_operations or []),
            success_criteria=s.success_criteria.model_dump(),
            notes=s.notes or "",
            is_active=True,
        )
        inserted += 1
    return inserted
