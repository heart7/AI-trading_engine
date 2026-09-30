# Strategy B validation on FIXTURE data (2026-09-28)

Class: REPORTED. FIXTURE prices and FIXTURE funding exercise the harness. They are not evidence about any real
market. Command: `python tools/run_harness.py --fixture --years 9 --reps 500 --sleeve <book>`.

| Step | B_short | B_long |
|---|---|---|
| 1 Data | PASS | PASS |
| 2 Null (Sharpe LB > 0, DSR, MinTRL) | FAIL: Sharpe +0.02, LB −0.61 | FAIL: Sharpe +0.05, LB −0.56 |
| 3 Ablation | FAIL | FAIL |
| 4 Cost tornado | FAIL: LB −0.65 at fees +50% | FAIL: LB −0.62 |
| 5 Vol deflation | FAIL: v never binds (same path on and off) | FAIL: same |
| 7 Regime robustness | FAIL | PASS |
| 8 σ* Monte Carlo | PASS (0.30, grid edge) | PASS (0.30, grid edge) |
| Trades / max DD | 70 / 2.1% | 126 / 3.9% |

Allowed mode for both books: PAPER, which is the only mode B can have below T3 anyway. What these results say:
- The harness, the perp replay (isolated margin, simultaneous-liquidation cap, conditional funding, funding time
  stop) and the verdict engine run end to end for both books with run records.
- Both books size small. At r_B = 0.20% NAV with 8–10% stop distances, positions are about 2–2.5% of NAV
  notional, so realised book volatility stays far below σ*, and volatility deflation (step 5) never binds.
- The fixture funding has a trend-following component, so B_long pays carry in uptrends. Two B_long
  positions left through the funding time stop.
