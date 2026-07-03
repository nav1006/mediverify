"""Plain dataclasses passed between phases. No framework, just records."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class Verdict(str, Enum):
    VERIFIED = "verified"
    REVIEW = "needs_review"
    WRONG = "wrong_number"
    DEAD = "dead_unverified"
    PENDING = "pending"


@dataclass
class Doctor:
    name: str
    phone: str          # raw, as it appears in the CSV
    address: str = ""
    raw: dict = field(default_factory=dict)  # any extra CSV columns


@dataclass
class Phase0Result:
    passed: bool
    line_status: str          # "live" | "dead" | "invalid"
    reason: str
    ported: Optional[bool] = None
    circle: Optional[str] = None
    e164: Optional[str] = None
    live_lookup: bool = False  # True if a real HLR call was made


@dataclass
class Phase1Result:
    outcome: str              # confirmed | wrong | recovered | noanswer | error
    name_match: bool
    confidence: float          # 0..1 from the identity judge
    transcript: str = ""
    summary: str = ""
    recovered_number: Optional[str] = None
    recording_url: Optional[str] = None
    call_id: Optional[str] = None
    decided_by: str = ""       # "llm" | "rules"
    live_call: bool = False


@dataclass
class Phase2Result:
    channel: str              # WhatsApp | SMS | none
    result: str               # profile-match | delivered | no-response | error
    detail: str
    name_match: bool = False
    live_send: bool = False


@dataclass
class VerificationRecord:
    doctor: Doctor
    p0: Optional[Phase0Result] = None
    p1: Optional[Phase1Result] = None
    p2: Optional[Phase2Result] = None
    score: int = 0
    verdict: Verdict = Verdict.PENDING

    def to_row(self) -> dict:
        """Flatten to a single dict for CSV/JSON output."""
        d = {
            "name": self.doctor.name,
            "phone": self.doctor.phone,
            "address": self.doctor.address,
            "score": self.score,
            "verdict": self.verdict.value,
        }
        if self.p0:
            d.update(
                e164=self.p0.e164 or "",
                line_status=self.p0.line_status,
                ported=self.p0.ported,
                circle=self.p0.circle or "",
                p0_reason=self.p0.reason,
            )
        if self.p1:
            d.update(
                call_outcome=self.p1.outcome,
                name_match=self.p1.name_match,
                call_confidence=round(self.p1.confidence, 2),
                decided_by=self.p1.decided_by,
                recovered_number=self.p1.recovered_number or "",
                recording_url=self.p1.recording_url or "",
                call_summary=self.p1.summary,
            )
        if self.p2:
            d.update(
                fallback_channel=self.p2.channel,
                fallback_result=self.p2.result,
                fallback_detail=self.p2.detail,
            )
        return d

    def to_json(self) -> dict:
        out = {"doctor": asdict(self.doctor), "score": self.score, "verdict": self.verdict.value}
        out["phase0"] = asdict(self.p0) if self.p0 else None
        out["phase1"] = asdict(self.p1) if self.p1 else None
        out["phase2"] = asdict(self.p2) if self.p2 else None
        return out
