# 0002 — Portfolio ES limit binds at about one position

Status: default applied 2026-09-29 (the principal asked the build to continue with its recommended defaults): **keep 1.5** as the spec says, and decide again once the harness has run on real history. No policy change, so nothing needs signing. Reversible.

Spec §5.5 and §7.4 cap portfolio ES97.5 (1 day) at `es975_mult × r_tier × NAV` = 1.5 × 0.75% = 1.125% NAV at T2.
One BTC position at 12.5% NAV with 3.5% daily vol already has a parametric ES of about 1.02% NAV. A second
correlated position (ρ_stress 0.85) takes the book to about 1.97%. So the limit allows roughly one full position,
while Appendix B.1 describes 2–3 concurrent positions and 25–35% gross.

FIXTURE harness comparison (9 years, 4 pairs, REPORTED class, not evidence):

| es975_mult | trades | Sharpe | max DD | size bound by ES |
|---|---|---|---|---|
| 1.5 (spec) | 141 | −0.05 | 9.8% | 24 of 144 entries |
| 3.0 | 142 | +0.03 | 9.7% | 0 |

On FIXTURE data the choice barely matters, because concurrent signals are rare. It will matter on real data,
where the large caps trend together. Options: keep 1.5, raise to 3.0 (the cluster open-risk cap of 2% still binds),
or decide after the harness runs on real history.
