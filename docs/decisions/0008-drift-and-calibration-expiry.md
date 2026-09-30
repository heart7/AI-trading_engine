# 0008 — Drift monitors, calibration expiry and what FAIL does

Status: decided by default in P9 (reversible, no owner action needed). Recorded 2026-09-30.

Spec §10.2 L3 names ECE, PSI and expiry dates, and says FAIL makes model outputs render ABSTAIN and halts the
learner's budget (§10.3a). It gives the ECE limits (5% warn, 8% fail) but not PSI thresholds, windows or expiry.

1. **Drift (A-DRIFT-PSI).** PSI of each model input, the 4h log return and the 4h high-low range, over the last 30
   days against the 180 days before, on ten quantile bins of the reference window. Below 0.10 is OK, below 0.25 WARN,
   otherwise FAIL (the usual industry bands). Signal outputs such as T are not monitored: a trend signal moves between
   regimes by design, so its PSI reads FAIL on healthy data.
2. **Calibration expiry (A-CALIBRATION-EXPIRY).** An ECE measurement expires 30 days after it was taken. Missing or
   expired renders non-authoritative on Bots & Models; ECE at or above `regime.ece_fail` is FAIL.
3. **What FAIL does.** The regime layer adds binding reason `DRIFT` (or `N1` for calibration) and its multiplier drops
   to 0, which is ABSTAIN. The hypothesis registry refuses new registrations while the learner is halted.
4. **FIXTURE never halts or clears anything.** The monitors run on the fixture universe so the screens show them
   working, but only an OBSERVED (certified) report can halt the learner.
