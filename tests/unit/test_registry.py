from pathlib import Path

import pytest
import yaml

from research.registry import registry as reg


def test_seeded_with_steps_2_to_5_pre_registered():
    items = reg.load()
    assert sorted(h["harness_step"] for h in items if h["id"].startswith("H-A-STEP")) == [2, 3, 4, 5]
    assert all(h["status"] == "PRE_REGISTERED" and h["run_ids"] == [] and h["mde"] for h in items)


def test_budget_enforced():
    items = reg.load()
    new = dict(items[0], id="H-A-EXTRA", budget_debit=40)
    with pytest.raises(reg.BudgetExceeded):
        reg.register(items, new, budget_per_year=40)
    ok = dict(items[0], id="H-A-EXTRA2", budget_debit=1)
    assert len(reg.register(items, ok, budget_per_year=40)) == len(items) + 1


def test_assumed_register_has_owner_and_review_date():
    doc = yaml.safe_load((Path(__file__).resolve().parents[2] / "policy" / "assumed_register.yaml").read_text())
    for a in doc["assumed"]:
        assert a["owner"] and a["review_by"] and a["id"].startswith("A-")
