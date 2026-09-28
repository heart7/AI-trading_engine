#!/usr/bin/env python3
"""Operator CLI for governance tasks that need the principal's hardware key.

  enroll     add a hardware public key (primary or backup) to policy/signers/signers.json
  statement  write the exact bytes to sign for an approval
  attach     combine a statement and its ssh-keygen signature into an approval record
  verify     verify an approval record against the enrolled keys
  coherence  print the coherence table for a policy and target mode
  activate   check coherence + approvals and report whether the policy can activate

Signing happens on the principal's own machine with the standard OpenSSH tool, which
requires a touch on the key:

  ssh-keygen -t ed25519-sk -O verify-required -C "uchfe-primary"   # once per key
  python3 tools/uchfe.py statement --action POLICY_ACTIVATE --policy policy/policy-10.4.0.yaml \\
      --rationale "why" -o stmt.json
  ssh-keygen -Y sign -f ~/.ssh/id_ed25519_sk -n uchfe-approval@v1 stmt.json
  python3 tools/uchfe.py attach stmt.json stmt.json.sig -o policy/approvals/<name>.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.governance import approvals as ap  # noqa: E402
from engine.governance import sshsig  # noqa: E402
from engine.policy.coherence import check_policy, summarise  # noqa: E402
from engine.policy.loader import load_policy  # noqa: E402


def cmd_enroll(a: argparse.Namespace) -> int:
    line = Path(a.pubkey).read_text().strip()
    pk = sshsig.parse_public_key_line(line)
    if not pk.hardware_backed:
        print("refused: only hardware (sk-) keys may be enrolled for approvals", file=sys.stderr)
        return 2
    doc = json.loads(ap.SIGNERS_FILE.read_text())
    signer = next(s for s in doc["signers"] if s["signer_id"] == a.signer)
    if any(k["key_id"] == a.key_id for k in signer["keys"]):
        print(f"refused: key_id {a.key_id} already enrolled", file=sys.stderr)
        return 2
    signer["keys"].append({"key_id": a.key_id, "public_key": " ".join(line.split()[:2]), "status": "ACTIVE",
                           "fingerprint": pk.fingerprint,
                           "enrolled_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    ap.SIGNERS_FILE.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"enrolled {a.key_id} {pk.fingerprint}")
    return 0


def _subject(a: argparse.Namespace) -> str:
    if a.policy:
        return load_policy(a.policy).hash
    if a.subject_hash:
        return a.subject_hash
    raise SystemExit("need --policy or --subject-hash")


def cmd_statement(a: argparse.Namespace) -> int:
    stmt = ap.statement(a.action, _subject(a), a.signer, a.rationale,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"))
    Path(a.output).write_bytes(ap.statement_bytes(stmt))
    print(f"wrote {a.output}; sign it with: ssh-keygen -Y sign -f <sk key> -n {ap.NAMESPACE} {a.output}")
    return 0


def cmd_attach(a: argparse.Namespace) -> int:
    stmt = json.loads(Path(a.statement).read_bytes())
    rec = dict(stmt, signature=Path(a.signature).read_text())
    Path(a.output).write_text(json.dumps(rec, indent=2) + "\n")
    print(f"wrote {a.output}")
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    rec = json.loads(Path(a.approval).read_text())
    try:
        va = ap.verify_approval(rec, ap.SignerRegistry.load(), expected_action=rec["action"],
                                expected_subject=rec["subject_hash"])
    except ap.ApprovalRefused as e:
        print(f"REFUSED {e}")
        return 1
    print(f"VALID signer={rec['signer_id']} key={va.key_id} {va.fingerprint} user_verified={va.user_verified}")
    return 0


def cmd_coherence(a: argparse.Namespace) -> int:
    r = check_policy(load_policy(a.policy))
    print(summarise(r, a.mode))
    return 0 if r.coherent_for(a.mode) else 1


def cmd_activate(a: argparse.Namespace) -> int:
    pol = load_policy(a.policy)
    recs = [json.loads(Path(p).read_text()) for p in a.approvals]
    try:
        act = ap.activate_policy(pol, recs, ap.SignerRegistry.load(), mode=a.mode)
    except ap.ApprovalRefused as e:
        print(f"NOT ACTIVATED {e}")
        return 1
    print(f"ACTIVATED {act.policy_version} {act.policy_hash} mode={act.mode}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enroll")
    e.add_argument("pubkey")
    e.add_argument("--key-id", required=True)
    e.add_argument("--signer", default="principal")
    e.set_defaults(fn=cmd_enroll)

    s = sub.add_parser("statement")
    s.add_argument("--action", required=True)
    s.add_argument("--policy")
    s.add_argument("--subject-hash")
    s.add_argument("--rationale", required=True)
    s.add_argument("--signer", default="principal")
    s.add_argument("-o", "--output", required=True)
    s.set_defaults(fn=cmd_statement)

    t = sub.add_parser("attach")
    t.add_argument("statement")
    t.add_argument("signature")
    t.add_argument("-o", "--output", required=True)
    t.set_defaults(fn=cmd_attach)

    v = sub.add_parser("verify")
    v.add_argument("approval")
    v.set_defaults(fn=cmd_verify)

    c = sub.add_parser("coherence")
    c.add_argument("--policy")
    c.add_argument("--mode", default="PAPER")
    c.set_defaults(fn=cmd_coherence)

    ac = sub.add_parser("activate")
    ac.add_argument("--policy")
    ac.add_argument("--mode", default="PAPER")
    ac.add_argument("approvals", nargs="*")
    ac.set_defaults(fn=cmd_activate)

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
