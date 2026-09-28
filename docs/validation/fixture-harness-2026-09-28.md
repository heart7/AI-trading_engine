# Validation harness on FIXTURE data — 2026-09-28

**Class: REPORTED.** Synthetic data (`FIXTURE_*`, certified: false). This run exercises the harness; it says nothing
about real-market edge. Command: `python3 tools/run_harness.py --fixture --years 9 --reps 500 --out runs/harness-fixture`.

| Step | Verdict | Deciding metric | Notes |
|---|---|---|---|
| 1 Data | PASS | coverage 100% | quality reports present for all 4 pairs |
| 2 Null test | FAIL | Sharpe −0.05, one-sided 95% LB −0.64, DSR 0.11 | the fixture has no persistent edge after costs, as intended → **endorsed fallback (§9.5): no validated edge** |
| 3 Ablation | FAIL | paired CIs include 0 for B, M and Z | on real data this would mean delete-by-proposal |
| 4 Cost tornado | FAIL | LB at fee +50%, q75: −0.77 | Kraken base fees (0.25% / 0.40%, ASSUMED) |
| 5 Vol deflation | FAIL | identical paths with v on and off | σ̂_book never exceeds σ* = 10% (anchor), so deflation never binds → remove-by-proposal candidate |
| 7 Regime robustness | PASS | max DD 5–8% in every label | bull/bear by 200-day MA; vol terciles |
| 8 σ* Monte Carlo | PASS* | σ* = 0.30 (grid edge) | *finding: with the loss ladder on, FLATTEN at DD 16% intercepts almost every path before 20%, so P(DD ≥ 20%) never exceeds 5% on the grid. Ladder off gives σ* = 0.10, which matches the §2.7 vol ceiling (~8.9%). Needs an owner decision before σ* is signed into policy |

Mode allowed by the evidence: **PAPER** (SHADOW needs steps 1–5, 7 and 8 to PASS).
P2b exit gate: verdicts on file. Step 2 failed, so the fallback is declared. On real data this is the point where the
project would continue only as infrastructure.
