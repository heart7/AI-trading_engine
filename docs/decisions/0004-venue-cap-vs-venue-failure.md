# 0004 — Stress scenario S4 (total loss of a venue) fails under the 40% venue cap

Status: default chosen 2026-09-29 (the principal asked the build to continue with its recommended defaults): **option 1**, `venue_exposure_max` 0.20 until a second UK venue is added. Because this changes the signed policy, it exists only as the unsigned proposal `policy/proposals/policy-10.4.1-venue-cap.yaml` with its stress run; it takes effect only if the principal signs it. Blocks SHADOW, not PAPER.

Spec §7.8 S4 assumes 100% loss of every asset at the largest-exposure venue and requires the loss to stay inside
the 20% hard drawdown budget. The policy allows up to `risk.venue_exposure_max = 0.40` of NAV on one venue
(positions plus the 10% operating buffer), and Kraken is the only execution venue. A policy-maximal book
therefore loses 40% in S4, at every tier. Every other scenario passes (worst: S7 at 9.3%).

Because the battery must pass for every policy proposal (INV-42) and coherence check C15 binds from SHADOW, no
policy with a 40% cap can go past PAPER.

Options:
1. Lower `venue_exposure_max` to 0.20 or less. S4 then passes at the limit. With the 10% operating buffer this
   leaves about 10% NAV for positions on one venue, which is well below Appendix B.1's 25–35% gross. A second
   UK venue (Coinbase Advanced or Bitstamp, §21 item 10a) would restore room: 2 × 20%.
2. Keep 40% and treat venue failure as a named risk outside the drawdown budget. S4 would then report rather
   than gate. This changes §7.8's PASS rule.
3. Lower the operating buffer and the cap together (for example buffer 5%, cap 20%).

The battery reports this as a finding. It does not choose between the options.
