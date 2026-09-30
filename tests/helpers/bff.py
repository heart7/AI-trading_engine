"""Shared paper session and a live BFF on an ephemeral port for P5 tests."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from functools import cache

from engine.bff.server import serve
from engine.bff.session import PaperSession


@cache
def session() -> PaperSession:
    return PaperSession.build()


def fresh_session() -> PaperSession:
    """A session tests may mutate (stream kill, injected events, reporter log)."""
    from dataclasses import replace

    from engine.common.incidents import IncidentLog
    s = session()
    return replace(s, incidents=IncidentLog(), reporter_log=[], extra_activity=[], cycle_cache={}, stream_alive=True,
                   stream_last_good=s.now)


class Live:
    def __init__(self, s: PaperSession):
        self.s = s
        self.srv = serve(s, port=0)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:  # noqa: S310 (localhost test server)
                raw = resp.read().decode()
                code = resp.status
        except urllib.error.HTTPError as e:
            raw, code = e.read().decode(), e.code
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, raw

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
