"""Confidence scoring + verdict.

The score is a transparent weighted sum, not a black box — easy to tune and
to defend in a demo. Identity signals dominate; reachability and circle match
are supporting evidence; recent porting and a clear 'wrong number' are
penalties.
"""
from __future__ import annotations

from .config import CONFIG
from .models import Phase0Result, Phase1Result, Phase2Result, Verdict


def score(p0: Phase0Result | None, p1: Phase1Result | None, p2: Phase2Result | None) -> int:
    # ── Full verification in Phase 1: the doctor confirmed on the call. ──
    # A confirmed identity from the voice call is the strongest signal we can
    # get — it's a direct human confirmation. Score it 100; no need for Phase 2.
    if p1 and p1.outcome == "confirmed" and p1.name_match:
        return 100

    s = 0

    # ── Reachability (Phase 0) — modest base; a live HLR is real signal but
    #    identity confirmation must still dominate the score. ──
    if p0 and p0.passed:
        s += 20                                  # HLR "ok" = reachable device
    if p0 and p0.circle and p0.circle != "Unknown":
        s += 5                                   # circle matches address

    # ── Identity (the part that actually verifies the doctor) ──
    if p1 and p1.name_match:
        s += 50
    if p2 and p2.name_match:
        s += 45
    if p1 and p1.outcome == "confirmed":
        s += 10
    if p1 and p1.confidence:
        s += int(p1.confidence * 10)             # judge certainty, up to +10
    if p1 and p1.recovered_number:
        s += 5

    # ── Penalties ──
    if p0 and p0.ported:
        s -= 10                                  # ported ⇒ mild uncertainty
    if p1 and p1.outcome == "wrong":
        s -= 30                                  # someone else answered

    return max(0, min(100, s))


def verdict(s: int, p1: Phase1Result | None, p0: Phase0Result | None = None) -> Verdict:
    # A *confident* wrong-number reading forces WRONG (someone else answered).
    # A low-confidence "wrong" (e.g. from a truncated call) shouldn't override
    # a reachable number — let the floor/score decide instead.
    if p1 and p1.outcome == "wrong" and p1.confidence >= 0.75:
        return Verdict.WRONG
    if s >= CONFIG.verified_threshold:
        return Verdict.VERIFIED
    if s >= CONFIG.review_threshold:
        return Verdict.REVIEW
    # Floor: a number HLR confirms is LIVE is worth a human callback even if the
    # ID call didn't complete — never bin a working number as dead.
    if p0 and p0.passed:
        return Verdict.REVIEW
    return Verdict.DEAD
