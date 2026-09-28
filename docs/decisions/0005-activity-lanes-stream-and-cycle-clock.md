# 0005 — Activity screen: lane count, stream transport and the paper cycle clock

Status: decided by default in P5 (reversible, no owner action needed). Recorded 2026-09-28.

1. **Lanes.** §16.8.1 P1 says "one lane per agent in §13.7 plus OMS, Protection verifier and Watchdog (10 lanes)".
   §13.7 lists 8 agents, so that is 11 lanes. The UI draws all 11 and drops none; the Allocator lane is greyed
   "inactive at T2" while the paper tier runs one sleeve.
2. **Stream.** §16.8.2 names `WS /v1/activity/stream`. The BFF is standard-library only, so the one-way live feed is
   Server-Sent Events at `GET /v1/activity/stream`. It is still read-only, and the "no controls" test accepts only GET
   routes plus `POST /v1/reporter/ask`. Replacing SSE with a WebSocket later changes no projection.
3. **Cycle clock (A-UI-CYCLE-CLOCK).** A paper replay has no wall clock inside a 4h cycle, so `t_offset_ms` for
   paper events comes from a deterministic FIXTURE clock that places each step inside its §13.4 window. Late events
   keep their real offset and are flagged `CYCLE_TIMEOUT` (tested by injection). Real timings replace this once the
   decision cycle runs as services.
4. **T band (A-T-BAND).** The spec requires T to be drawn banded where it depends on estimated inputs. B and Z are
   exact functions of closes; M uses the EWMA volatility estimate. The band is T recomputed with that estimate scaled
   by 0.8 and 1.25. Owner: principal; review with the step 8 sigma* work.
5. **Fixture session (A-UI-FIXTURE-SESSION).** The UI's paper session replays the fixture universe with
   `mu_q_daily = 0.001`, the golden-replay value, so the screens show trades. It is FIXTURE data and every figure is
   hatched with `certified: false`.
