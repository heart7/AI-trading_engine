
import pytest
import yaml

from engine.common.canonical import canonical_json, content_hash
from engine.common.schemas import SchemaError
from engine.policy.coherence import EvidenceContext, check_policy
from engine.policy.loader import POLICY_DIR, load_policy, parse_policy

RAW = (POLICY_DIR / "policy-10.4.0.yaml").read_text()


def mutate(fn):
    doc = yaml.safe_load(RAW)
    # PyYAML 1.1 reads 1.0e8 as a string; restore numbers before dumping the variant.
    doc["universe"]["B"]["min_vol_usd_30d_median"] = 1.0e8
    doc["universe"]["B"]["min_oi_usd"] = 5.0e7
    fn(doc)
    return parse_policy(yaml.safe_dump(doc))


def test_policy_loads_and_matches_spec_values():
    p = load_policy()
    assert p.version == "10.4.0"
    assert p.get("base_currency") == "USD"
    assert p.get("governance.approvals") == {"signers_required": 1, "cooling_off_hours": 0,
                                             "reauth": "webauthn", "rationale_required": True}
    assert p.get("risk.ladder.dd.terminate") == 0.20
    assert p.get("venues.declared_residence") == "GB"
    assert p.get("venues.execution_allowed.A_long") == ["kraken-spot"]
    assert p.get("universe.B.min_vol_usd_30d_median") == 1.0e8


def test_policy_hash_is_content_addressed():
    assert load_policy().hash == content_hash(yaml.load(RAW, Loader=__import__("engine.policy.loader", fromlist=["_Loader"])._Loader))
    reformatted = parse_policy(RAW.replace("timeframe: 4h", "timeframe:    4h   # same"))
    assert reformatted.hash == load_policy().hash


def test_canonical_json_normalises_int_like_floats():
    assert canonical_json({"b": 1.0, "a": [2.50, 3]}) == '{"a":[2.5,3],"b":1}'
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


@pytest.mark.parametrize("path,value", [
    (("base_currency",), "GBP"),
    (("perps", "margin_mode"), "CROSS"),
    (("venues", "geo_circumvention"), "allowed"),
    (("venues", "hold_exchange_tokens"), True),
    (("governance", "approvals", "rationale_required"), False),
    (("unknown_field",), 1),
])
def test_schema_rejects_forbidden_values(path, value):
    def f(d):
        node = d
        for k in path[:-1]:
            node = node[k]
        node[path[-1]] = value
    with pytest.raises(SchemaError):
        mutate(f)


def test_shipped_policy_coherent_for_paper_but_not_shadow():
    r = check_policy(load_policy())
    assert r.coherent_for("PAPER")
    assert not r.coherent_for("SHADOW")
    assert {c.id for c in r.failures("SHADOW")} == {"C08", "C13", "C15"}
    assert {row["id"] for row in r.table("PAPER") if row["status"] == "DEFERRED"} == {"C08", "C13", "C15"}


def test_margin_projection_matches_appendix_b2():
    c07 = next(c for c in check_policy(load_policy()).checks if c.id == "C07")
    assert "0.0095 NAV" in c07.detail and "effective B slots 10" in c07.detail


@pytest.mark.parametrize("fn,failing", [
    (lambda d: d["stops"].update(k_stop=3.0), "C01"),
    (lambda d: d["signal"].update(T_entry=1.5), "C02"),
    (lambda d: d["cost"].update(cost_R_max=1.2), "C03"),
    (lambda d: d["tiers"]["T2"].update(nav_down=40000), "C04"),
    (lambda d: d["tiers"]["T3"].update(nav_up=20000), "C05"),
    (lambda d: d["tiers"]["T3"]["r"].update(B=0.01), "C06"),
    (lambda d: d["sizing"]["cluster"].update(open_risk_cap=0.004), "C09"),
    (lambda d: d["risk"]["ladder"].update(daily_stop=0.01), "C10"),
    (lambda d: d["sizing"].update(rebalance_band=1.5), "C11"),
    (lambda d: d["regime"].update(authority="T1"), "C12"),
    (lambda d: d["venues"]["execution_allowed"].update(A_long=["kraken-spot", "ccxt-generic"]), "C14"),
    (lambda d: d["risk"]["ladder"]["dd"].update(suspend_downgrade=0.07), "C16"),
])
def test_each_structural_check_fails_in_paper(fn, failing):
    r = check_policy(mutate(fn))
    assert failing in {c.id for c in r.failures("PAPER")}
    assert not r.coherent_for("PAPER")


def test_evidence_checks_pass_with_evidence_on_file():
    def f(d):
        d["sizing"]["sigma_star"] = {"value": 0.10, "anchor": 0.10, "mc_run_id": "mc-1"}
    p = mutate(f)
    ctx = EvidenceContext(
        mc_runs={"mc-1": {"verdict": "PASS", "p_dd20_1y": 0.03}},
        stress_runs={"sb-1": {"verdict": "PASS", "policy_hash": p.hash}}, stress_run_id="sb-1",
        venue_instances={"kraken-spot": [{"state": "TRADE_ENABLED", "access_records": {"spot": "2099-01-01T00:00:00Z"}}]},
    )
    assert check_policy(p, ctx).coherent_for("LIVE")
    # expired access record, or a stress run for another policy hash, fails again
    ctx.venue_instances["kraken-spot"][0]["access_records"]["spot"] = "2000-01-01T00:00:00Z"
    assert "C13" in {c.id for c in check_policy(p, ctx).failures("LIVE")}
    ctx.stress_runs["sb-1"]["policy_hash"] = "0" * 64
    assert "C15" in {c.id for c in check_policy(p, ctx).failures("SHADOW")}


def test_mc_run_with_excess_ruin_probability_fails():
    p = mutate(lambda d: d["sizing"].update(sigma_star={"value": 0.12, "anchor": 0.10, "mc_run_id": "mc-2"}))
    ctx = EvidenceContext(mc_runs={"mc-2": {"verdict": "PASS", "p_dd20_1y": 0.09}})
    assert "C08" in {c.id for c in check_policy(p, ctx).failures("SHADOW")}
