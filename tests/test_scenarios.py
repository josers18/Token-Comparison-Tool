from pathlib import Path
import pytest

from token_compare.scenarios import load_all, load_file


FIXTURES = Path(__file__).parent / "fixtures"


def test_load_file_parses_fixture():
    s = load_file(FIXTURES / "scenario_valid.yaml")
    assert s.id == "s99_fixture"
    assert s.difficulty == "simple"
    assert s.success_criteria.must_contain == ["count"]


def test_load_all_returns_sorted_by_id(tmp_path):
    (tmp_path / "b.yaml").write_text(
        "id: s02\ntitle: b\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    (tmp_path / "a.yaml").write_text(
        "id: s01\ntitle: a\ncategory: c\ndifficulty: simple\n"
        "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
    )
    got = load_all(tmp_path)
    assert [s.id for s in got] == ["s01", "s02"]


def test_load_all_rejects_duplicate_ids(tmp_path):
    for name in ("x.yaml", "y.yaml"):
        (tmp_path / name).write_text(
            "id: dup\ntitle: t\ncategory: c\ndifficulty: simple\n"
            "prompt: x\nexpected_operations: []\nsuccess_criteria:\n  must_contain: []\n"
        )
    with pytest.raises(ValueError, match="duplicate"):
        load_all(tmp_path)


def test_real_catalog_has_six_scenarios():
    repo_root = Path(__file__).parent.parent
    scenarios = load_all(repo_root / "scenarios")
    assert len(scenarios) == 6
    ids = {s.id for s in scenarios}
    assert ids == {
        "s01_soql_top_accounts",
        "s02_unified_profile_lookup",
        "s03_trade_volume_breakdown",
        "s04_open_cases_by_priority",
        "s05_opportunity_pipeline_report",
        "s06_customer_360_displaytech",
    }
