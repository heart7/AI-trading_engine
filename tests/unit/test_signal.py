import math

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st

from engine.signal.trend import SignalParams, signal_at, signal_series
from tests.helpers.fixtures import arrays, policy

P = SignalParams.from_policy(policy())


def test_series_matches_scalar_path():
    a = arrays("FIXTURE_BTC", 2400, 7)
    s = signal_series(a["h"], a["l"], a["c"], P)
    for i in [0, 1079, 1080, 1081, 1500, 2000, 2399]:
        x = signal_at(a["h"], a["l"], a["c"], i, P)
        if x.T is None:
            assert math.isnan(s["T"][i])
        else:
            for k in "BMZT":
                assert abs(getattr(x, k) - s[k][i]) < 1e-12


def test_components_bounded_and_history_rule():
    a = arrays("FIXTURE_ETH", 3000, 8, 0.05)
    s = signal_series(a["h"], a["l"], a["c"], P)
    assert np.all(np.isnan(s["T"][:P.min_history_bars]))  # < 180 days -> ABSTAIN INSUFFICIENT_HISTORY
    for k in "BMZT":
        v = s[k][~np.isnan(s[k])]
        assert v.size and v.min() >= -1 and v.max() <= 1


@settings(max_examples=15, deadline=None)
@given(cut=st.integers(min_value=1100, max_value=2300), extra=st.integers(min_value=1, max_value=400))
def test_causality_future_bars_never_change_the_past(cut, extra):
    a = arrays("FIXTURE_XRP", 2800, 9)
    full = signal_series(a["h"], a["l"], a["c"], P)
    part = signal_series(a["h"][:cut + extra], a["l"][:cut + extra], a["c"][:cut + extra], P)
    head = signal_series(a["h"][:cut], a["l"][:cut], a["c"][:cut], P)
    for k in "BMZT":
        np.testing.assert_array_equal(head[k], part[k][:cut])
        np.testing.assert_array_equal(head[k], full[k][:cut])


def test_breakout_uses_closes_not_highs():
    # a spike in highs alone must not create a breakout (regression for the v8 bug)
    a = arrays("FIXTURE_BTC", 1300, 11)
    h = a["h"].copy()
    c = a["c"]
    i = 1250
    base = signal_at(h, a["l"], c, i, P).B
    h[i - 100] = c.max() * 10  # absurd intrabar high inside every lookback window
    assert signal_at(h, a["l"], c, i, P).B == base


def test_lookbacks_come_from_policy():
    import yaml

    from engine.policy.loader import POLICY_DIR, parse_policy
    raw = (POLICY_DIR / "policy-10.4.0.yaml").read_text().replace("breakout_lookbacks_d: [30, 90, 180]", "breakout_lookbacks_d: [20]")
    p2 = SignalParams.from_policy(parse_policy(raw).doc)
    assert p2.breakout_lookbacks_d == (20,) and yaml
