"""Read-only BFF over HTTP (spec §15.3). Standard library only; binds to localhost by default.

Every route is a GET projection except POST /v1/reporter/ask, which answers a question and changes no engine state.
Any other method is 405. There is no route that places, changes or approves anything: the "no controls" test scans
ROUTES and the UI's fetch calls.

`?cursor=<ISO time>` on any GET masks the session server-side (playback): the response cannot contain data
timestamped after the cursor.

The activity stream is Server-Sent Events at GET /v1/activity/stream (§16.8.2 names a WebSocket; SSE is the
standard-library equivalent for a one-way feed, decision 0005).

  python -m engine.bff.server --port 8710       # then open http://127.0.0.1:8710/
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import re
from collections.abc import Callable
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from engine.bff import projections as P
from engine.bff import reporter
from engine.bff.session import PaperSession

STATIC = Path(__file__).resolve().parents[1] / "ui" / "static"
MAX_BODY = 4096


def _dt(q: dict, k: str) -> datetime | None:
    v = q.get(k)
    return datetime.fromisoformat(v) if v else None


def _int(q: dict, k: str, default: int | None) -> int | None:
    v = q.get(k)
    return int(v) if v not in (None, "") else default


Handler = Callable[[PaperSession, dict[str, str], dict[str, str]], Any]
ROUTES: list[tuple[str, str, Handler]] = [
    ("GET", r"/v1/topbar", lambda s, m, q: P.topbar(s)),
    ("GET", r"/v1/fund-room", lambda s, m, q: P.fund_room(s)),
    ("GET", r"/v1/activity/cycles", lambda s, m, q: P.activity_cycles(s, _int(q, "limit", 6))),
    ("GET", r"/v1/activity/cycles/(?P<cycle_id>[^/]+)", lambda s, m, q: P.activity_cycle(s, m["cycle_id"], _int(q, "cursor_ms", None))),
    ("GET", r"/v1/activity/pairs/(?P<pair>[^/]+)", lambda s, m, q: P.activity_pair(s, m["pair"], _int(q, "bars", 180))),
    ("GET", r"/v1/activity/universe", lambda s, m, q: P.activity_universe(s)),
    ("GET", r"/v1/activity/funnel", lambda s, m, q: P.funnel(s, _dt(q, "from"), _dt(q, "to"))),
    ("GET", r"/v1/activity/timing", lambda s, m, q: P.timing(s, _int(q, "days", 30))),
    ("GET", r"/v1/activity/emissions", lambda s, m, q: P.emissions_projection(s, _int(q, "days", 30))),
    ("GET", r"/v1/markets/(?P<pair>[^/]+)", lambda s, m, q: P.markets(s, m["pair"], _int(q, "bars", 360), q.get("tf", "4h"))),
    ("GET", r"/v1/strategy", lambda s, m, q: P.strategy(s)),
    ("GET", r"/v1/strategy/router", lambda s, m, q: P.router(s)),
    ("GET", r"/v1/probability/(?P<pair>[^/]+)", lambda s, m, q: P.probability(s, m["pair"])),
    ("GET", r"/v1/pnl", lambda s, m, q: P.pnl(s)),
    ("GET", r"/v1/outcomes", lambda s, m, q: P.outcomes(s)),
    ("GET", r"/v1/outcomes/playback", lambda s, m, q: P.playback(s, q.get("pair", "BTC"), _dt(q, "at") or s.clock)),
    ("GET", r"/v1/data", lambda s, m, q: P.data(s)),
    ("GET", r"/v1/bots", lambda s, m, q: P.bots(s)),
    ("GET", r"/v1/risk", lambda s, m, q: P.risk(s)),
    ("GET", r"/v1/incidents", lambda s, m, q: P.incidents(s)),
    ("GET", r"/v1/venues", lambda s, m, q: P.venues(s)),
    ("GET", r"/v1/governance", lambda s, m, q: P.governance(s)),
    ("GET", r"/v1/validation", lambda s, m, q: P.validation(s)),
    ("GET", r"/v1/settings", lambda s, m, q: P.settings(s)),
    ("GET", r"/v1/settings/exchanges", lambda s, m, q: P.exchanges(s)),
    ("POST", r"/v1/reporter/ask", None),  # handled in do_POST: the only non-GET route, and it mutates no engine state
]
_COMPILED = [(meth, re.compile("^" + pat + "$"), h) for meth, pat, h in ROUTES]


def resolve(method: str, path: str) -> tuple[Handler | None, dict[str, str]] | None:
    for meth, rx, h in _COMPILED:
        m = rx.match(path)
        if m and meth == method:
            return h, {k: unquote(v) for k, v in m.groupdict().items()}
    return None


def make_handler(session: PaperSession) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        server_version = "uchfe-bff/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, doc: Any) -> None:
            self._send(code, json.dumps(doc, default=_default).encode())

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(u.query).items()}
            if not u.path.startswith("/v1/"):
                return self._static(u.path)
            if u.path == "/v1/activity/stream":
                return self._stream()
            r = resolve("GET", u.path)
            if r is None:
                return self._json(404, {"error": "not found"})
            s = session.mask(_dt(q, "cursor"))
            try:
                self._json(200, r[0](s, r[1], q))
            except KeyError as e:
                self._json(404, {"error": f"unknown {e}"})
            except ValueError as e:
                self._json(400, {"error": str(e)})

        def do_POST(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            if u.path != "/v1/reporter/ask":
                return self._json(405, {"error": "read-only BFF: no mutating routes"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "question too long"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                question = str(body["question"])[:500]
            except (ValueError, KeyError):
                return self._json(400, {"error": "body must be {\"question\": \"...\"}"})
            self._json(200, reporter.ask(session, question))

        def _refuse(self) -> None:
            self._json(405, {"error": "read-only BFF: no mutating routes"})

        do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815

        def _stream(self) -> None:
            if not session.stream_alive:
                return self._json(503, {"error": "activity stream down", "last_good": session.stream_last_good.isoformat()
                                        if session.stream_last_good else None})
            cyc = P.activity_cycles(session, 1)["cycles"][0]
            doc = P.activity_cycle(session, cyc["cycle_id"])
            body = "".join(f"event: activity\ndata: {json.dumps(e, default=_default)}\n\n" for e in doc["events"])
            body += "event: end\ndata: {}\n\n"
            self._send(200, body.encode(), "text/event-stream")

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            f = (STATIC / rel).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            self._send(200, f.read_bytes(), ctype)

    return H


def _default(o: Any) -> Any:
    import numpy as np
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def serve(session: PaperSession, host: str = "127.0.0.1", port: int = 8710) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(session))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--harness", default="runs/harness-fixture/harness.json", help="harness run record to show on Validation")
    a = ap.parse_args()
    srv = serve(PaperSession.build(harness_path=a.harness), a.host, a.port)
    print(f"UCHFE paper UI on http://{a.host}:{srv.server_address[1]}/ (PAPER, FIXTURE data, read-only)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
