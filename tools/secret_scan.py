#!/usr/bin/env python3
"""Fail if anything that looks like a credential is committed (spec §17, conduct rule 0.2.7)."""
from __future__ import annotations

import re
import subprocess

PATTERNS = {
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "slack token": re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    "generic api secret assignment": re.compile(
        r"(?i)\b(api[_-]?secret|secret[_-]?key|api[_-]?key|passphrase)\b\s*[:=]\s*['\"][A-Za-z0-9+/=_-]{24,}['\"]"),
}
ALLOW = {"tools/secret_scan.py"}


def main() -> int:
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.split()
    hits = []
    for f in files:
        if f in ALLOW:
            continue
        try:
            text = open(f, encoding="utf-8").read()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for name, rx in PATTERNS.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{f}:{line}: {name}")
    for h in hits:
        print(h)
    print(f"secret scan: {len(files)} files, {len(hits)} findings")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
