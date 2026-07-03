"""CLI entrypoint.

Two commands:

  check  — verify ONE phone number, printed phase-by-phase. Best for a live demo.
      python -m mediverify.cli check +919876543210 --name "Dr. Ananya Sharma"
      python -m mediverify.cli check +919876543210 --name "Dr. A" --repeat
      python -m mediverify.cli check              # prompts for the number

  verify — batch mode over a CSV (name, phone, [address]).
      python -m mediverify.cli verify doctors.csv --out runs/today
      python -m mediverify.cli verify doctors.csv --mock --limit 20
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .agent import load_doctors, run_batch, summarize, verify_one, write_outputs
from .config import CONFIG
from .models import Doctor, Verdict

# ── pretty print helpers ────────────────────────────────────────────────────
_BAR = "─" * 56

_VERDICT_TAG = {
    Verdict.VERIFIED: "[ VERIFIED ]",
    Verdict.REVIEW:   "[ NEEDS REVIEW ]",
    Verdict.WRONG:    "[ WRONG NUMBER ]",
    Verdict.DEAD:     "[ DEAD / UNVERIFIED ]",
    Verdict.PENDING:  "[ PENDING ]",
}


def _mode_banner() -> None:
    mode = "MOCK (nothing is dialed)" if CONFIG.mock else "LIVE"
    print(f"\nMediVerify · mode = {mode}")
    print(f"  Phase 0 HLR   : {'live' if CONFIG.phase0_live() else 'mock'}")
    print(f"  Phase 1 call  : {'live' if CONFIG.phase1_live() else 'mock'}  "
          f"(judge: {'LLM + rules' if CONFIG.llm_live() else 'rules only'})")
    print(f"  Phase 2 WA/SMS: {'live' if CONFIG.phase2_live() else 'mock'}")


def _print_single(doctor: Doctor, json_out: bool = False):
    """Run one doctor through the pipeline.

    Pretty-prints phase-by-phase unless json_out=True, in which case it prints
    only the structured record (the same shape that will feed Zoho next phase).
    Returns the VerificationRecord either way.
    """
    if json_out:
        rec = verify_one(doctor)
        print(json.dumps(rec.to_json(), indent=2, ensure_ascii=False))
        return rec

    print("\n" + _BAR)
    print(f"  INPUT   {doctor.name or '(no name given)'}")
    print(f"          {doctor.phone}")
    if doctor.address:
        print(f"          {doctor.address}")
    print(_BAR)

    rec = verify_one(doctor)  # phases logged via logging at INFO

    # ── Phase 0 ──
    p0 = rec.p0
    print(f"\n  PHASE 0 · Pre-screen")
    if p0:
        print(f"     normalized : {p0.e164 or '—'}")
        print(f"     line       : {p0.line_status}")
        print(f"     circle     : {p0.circle or '—'}"
              f"{'  · recently ported ⚠' if p0.ported else ''}")
        print(f"     result     : {p0.reason}")
        if not p0.passed:
            print(f"\n     ↳ stopped here — number never dialed.")

    # ── Phase 1 ──
    if rec.p1:
        p1 = rec.p1
        print(f"\n  PHASE 1 · AI voice call")
        print(f"     outcome    : {p1.outcome}")
        print(f"     name match : {p1.name_match}")
        print(f"     confidence : {p1.confidence:.2f}  (decided by {p1.decided_by or '—'})")
        if p1.recovered_number:
            print(f"     recovered  : {p1.recovered_number}")
        if p1.recording_url:
            print(f"     recording  : {p1.recording_url}")
        if p1.transcript:
            print(f"     transcript :")
            for line in p1.transcript.splitlines():
                print(f"        {line}")
        elif p1.summary:
            print(f"     summary    : {p1.summary}")

    # ── Phase 2 ──
    if rec.p2:
        p2 = rec.p2
        print(f"\n  PHASE 2 · Fallback ({p2.channel})")
        print(f"     result     : {p2.result}")
        print(f"     detail     : {p2.detail}")

    # ── Verdict ──
    print("\n" + _BAR)
    print(f"  SCORE   {rec.score}/100        {_VERDICT_TAG.get(rec.verdict, '')}")
    print(_BAR + "\n")
    return rec


def _cmd_check(args) -> int:
    json_out = getattr(args, "json_out", False)
    if not json_out:
        _mode_banner()
    repeat = getattr(args, "repeat", False)

    def run_number(number: str, name: str, address: str) -> None:
        number = number.strip()
        if not number:
            return
        if not number.startswith("+") and not json_out:
            print("  ⚠ tip: include the country code, e.g. +91 for India.")
        _print_single(Doctor(name=name.strip(), phone=number, address=address.strip()),
                      json_out=json_out)

    # number supplied on the command line
    if args.number:
        run_number(args.number, args.name or "", args.address or "")
        if not repeat:
            return 0

    # interactive loop (default when no number given, or with --repeat)
    if not json_out:
        print("\n  Enter a phone number with country code (blank line or 'q' to quit).")
    while True:
        try:
            number = input("" if json_out else "\n  number> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if number.lower() in {"", "q", "quit", "exit"}:
            break
        name = input("" if json_out else "  name (optional)> ").strip()
        run_number(number, name, "")
    if not json_out:
        print("  done.\n")
    return 0


def _print_summary(s: dict) -> None:
    print("\n" + "=" * 52)
    print("  BATCH SUMMARY")
    print("=" * 52)
    print(f"  Total doctors      : {s['total']}")
    print(f"  Calls placed       : {s['calls_placed']}")
    print(f"  Calls avoided (P0) : {s['calls_avoided']}")
    print(f"  Numbers recovered  : {s['numbers_recovered']}")
    print("  ----------------------------------------------")
    for v, n in sorted(s["by_verdict"].items()):
        print(f"  {v:<18}: {n}")
    print("=" * 52 + "\n")


def _cmd_verify(args) -> int:
    _mode_banner()
    doctors = load_doctors(args.csv)
    if args.limit:
        doctors = doctors[: args.limit]
    print(f"  Loaded {len(doctors)} doctors from {args.csv}\n")
    records = run_batch(doctors)
    paths = write_outputs(records, args.out)
    _print_summary(summarize(records))
    print("  Outputs:")
    for k, pth in paths.items():
        print(f"   • {k}: {pth}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="mediverify", description="Local doctor phone-verification agent.")
    p.add_argument("--mock", action="store_true", help="Force mock mode (no API calls)")
    p.add_argument("-q", "--quiet", action="store_true", help="Less step logging")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="Verify ONE phone number, phase-by-phase (demo mode).")
    c.add_argument("number", nargs="?", default="", help="Phone number WITH country code, e.g. +919876543210")
    c.add_argument("--name", default="", help="Doctor name to verify against (optional)")
    c.add_argument("--address", default="", help="Clinic address, used for circle match (optional)")
    c.add_argument("--repeat", action="store_true", help="Keep prompting for more numbers")
    c.add_argument("--json", dest="json_out", action="store_true",
                   help="Print the structured record (Zoho-ready) instead of the pretty view")
    c.set_defaults(func=_cmd_check)

    v = sub.add_parser("verify", help="Run the pipeline over a CSV.")
    v.add_argument("csv", help="Input CSV with columns: name, phone, [address]")
    v.add_argument("--out", default="runs/latest", help="Output directory")
    v.add_argument("--limit", type=int, default=0, help="Only process first N rows")
    v.set_defaults(func=_cmd_verify)

    args = p.parse_args(argv)
    if args.mock:
        os.environ["MOCK"] = "true"
        CONFIG.mock = True

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO, format="%(message)s")
    # In single-number demo mode, keep the narration clean: silence INFO logs
    # so our formatted phase output is the only thing on screen.
    if args.cmd == "check":
        logging.getLogger("mediverify").setLevel(logging.WARNING)

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
