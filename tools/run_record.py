#!/usr/bin/env python3
"""Run a command and write a run record (conduct rule 0.2.1: command, environment hash, timestamp, output).

  python3 tools/run_record.py --out runs/p0/tests.json -- python3 -m pytest -q tests
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.common.canonical import content_hash  # noqa: E402

PACKAGES = ["numpy", "pandas", "pydantic", "jsonschema", "PyYAML", "cryptography", "pytest", "hypothesis"]


def env() -> dict:
    pk = {}
    for p in PACKAGES:
        try:
            pk[p] = metadata.version(p)
        except metadata.PackageNotFoundError:
            pk[p] = None
    return {"python": sys.version.split()[0], "implementation": platform.python_implementation(),
            "platform": platform.platform(), "packages": pk}


def git(*args: str) -> str | None:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--summary-json", help="JSON file produced by the command to embed as summary")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    started = datetime.now(timezone.utc)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    finished = datetime.now(timezone.utc)
    output = proc.stdout + proc.stderr
    e = env()
    summary = {}
    if a.summary_json and Path(a.summary_json).exists():
        summary = json.loads(Path(a.summary_json).read_text())
    rec = {
        "run_id": f"run-{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
        "command": " ".join(cmd), "env_hash": content_hash(e), "env": e,
        "started_at": started.isoformat(timespec="seconds"), "finished_at": finished.isoformat(timespec="seconds"),
        "exit_code": proc.returncode, "git_commit": git("rev-parse", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain")), "summary": summary,
        "output_sha256": hashlib.sha256(output.encode()).hexdigest(), "output_tail": output[-4000:],
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rec, indent=2) + "\n")
    sys.stdout.write(output)
    print(f"run record: {a.out} ({rec['run_id']}, exit {proc.returncode})")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
