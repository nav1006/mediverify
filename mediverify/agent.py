"""The agent: orchestrates Phase 0 → 1 → 2 with escalation, per doctor.

Escalation rules (the whole point of the design):
  • Phase 0 fails  → stop. Never dial a dead/invalid number.
  • Phase 1 confirmed or wrong → stop. We have our answer.
  • Phase 1 inconclusive (noanswer/recovered/error) → run Phase 2.
"""
from __future__ import annotations

import csv
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

from .config import CONFIG
from .models import Doctor, VerificationRecord, Verdict
from .phase0_prescreen import run_phase0
from .phase1_voice import run_phase1
from .phase2_fallback import run_phase2
from .scoring import score, verdict

log = logging.getLogger("mediverify")


def load_doctors(csv_path: str | Path) -> list[Doctor]:
    rows: list[Doctor] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
        need = {"name", "phone"}
        if not need.issubset(cols):
            raise ValueError(f"CSV must have at least columns: name, phone. Found: {reader.fieldnames}")
        for r in reader:
            rows.append(Doctor(
                name=r[cols["name"]].strip(),
                phone=r[cols["phone"]].strip(),
                address=(r[cols["address"]].strip() if "address" in cols else ""),
                raw=dict(r),
            ))
    return rows


def verify_one(doctor: Doctor) -> VerificationRecord:
    rec = VerificationRecord(doctor=doctor)

    # Phase 0
    rec.p0 = run_phase0(doctor)
    log.info("P0 %-22s %s | %s", doctor.name, rec.p0.line_status, rec.p0.reason)
    if not rec.p0.passed:
        rec.score = score(rec.p0, None, None)
        rec.verdict = verdict(rec.score, None, rec.p0)
        return rec

    e164 = rec.p0.e164 or doctor.phone

    # Phase 1
    rec.p1 = run_phase1(doctor, e164)
    log.info("P1 %-22s %s (by %s, conf %.2f)", doctor.name,
             rec.p1.outcome, rec.p1.decided_by or "-", rec.p1.confidence)

    # Phase 2 only if inconclusive
    if rec.p1.outcome in {"noanswer", "recovered", "error"}:
        rec.p2 = run_phase2(doctor, e164)
        log.info("P2 %-22s %s/%s", doctor.name, rec.p2.channel, rec.p2.result)

    rec.score = score(rec.p0, rec.p1, rec.p2)
    rec.verdict = verdict(rec.score, rec.p1, rec.p0)
    log.info("→  %-22s score=%d verdict=%s", doctor.name, rec.score, rec.verdict.value)
    return rec


def run_batch(doctors: Iterable[Doctor]) -> list[VerificationRecord]:
    doctors = list(doctors)
    # Live calls should stay serial (max_workers=1) to respect dialer limits;
    # mock mode can parallelize freely.
    workers = CONFIG.max_workers if CONFIG.mock else max(1, CONFIG.max_workers)
    if workers <= 1:
        return [verify_one(d) for d in doctors]
    out: list[VerificationRecord] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(verify_one, d): d for d in doctors}
        for fut in as_completed(futs):
            out.append(fut.result())
    return out


def write_outputs(records: list[VerificationRecord], out_dir: str | Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Full results CSV
    rows = [r.to_row() for r in records]
    fields: list[str] = []
    for row in rows:
        for k in row:
            if k not in fields:
                fields.append(k)
    results_csv = out_dir / "results.csv"
    with open(results_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)

    # JSON (full detail incl. transcripts)
    results_json = out_dir / "results.json"
    with open(results_json, "w", encoding="utf-8") as f:
        json.dump([r.to_json() for r in records], f, indent=2, ensure_ascii=False)

    # Sales shortlist: verified + recovered numbers — the pre-qualified list
    shortlist = out_dir / "sales_shortlist.csv"
    with open(shortlist, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "best_number", "address", "verdict", "score"])
        for r in records:
            if r.verdict in {Verdict.VERIFIED, Verdict.REVIEW}:
                best = (r.p1.recovered_number if (r.p1 and r.p1.recovered_number)
                        else (r.p0.e164 if r.p0 else r.doctor.phone))
                w.writerow([r.doctor.name, best, r.doctor.address, r.verdict.value, r.score])

    return {"results_csv": str(results_csv), "results_json": str(results_json),
            "shortlist": str(shortlist)}


def summarize(records: list[VerificationRecord]) -> dict:
    counts: dict[str, int] = {}
    for r in records:
        counts[r.verdict.value] = counts.get(r.verdict.value, 0) + 1
    recovered = sum(1 for r in records if r.p1 and r.p1.recovered_number)
    calls_placed = sum(1 for r in records if r.p1 is not None)
    return {"total": len(records), "by_verdict": counts,
            "numbers_recovered": recovered, "calls_placed": calls_placed,
            "calls_avoided": len(records) - calls_placed}
