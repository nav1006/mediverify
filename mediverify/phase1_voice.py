"""Phase 1 — AI voice call (Bland.ai) + identity judge (LLM with rules fallback).

Flow:
  1. POST /v1/calls with a task prompt + structured analysis schema.
  2. Poll GET /v1/calls/{id} until completed (or use a webhook in production).
  3. Feed the transcript to the identity judge:
       - primary: an LLM that returns strict JSON
       - fallback: keyword/rule matching, used if the LLM is unavailable
         or returns unparseable output.

The judge is what turns "we reached someone" into "we reached the right
doctor", and it's deliberately separate from the telephony so you can swap
Bland for Vapi/Twilio without touching the decision logic.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

import requests

from .config import CONFIG
from .models import Doctor, Phase1Result

log = logging.getLogger("mediverify")

# ── Call script ───────────────────────────────────────────────────────────
TASK_TEMPLATE = (
    "You are a polite verification assistant for a medical directory. "
    "You are calling to confirm contact details for {name}. "
    "Respond in whatever language the person speaks (Hindi, English, or mixed). "
    "This is NOT a sales call — do not pitch anything. "
    "Goal: confirm whether this number reaches {name} or {name}'s clinic/office. "
    "Start by asking, in a warm tone, whether you've reached {name} or their clinic. "
    "If yes, thank them briefly and end. "
    "If they say it's the wrong number, politely ask if they happen to know "
    "{name}'s current number, then thank them and end. "
    "Keep the call under 60 seconds. Never reveal you are an Ael; just be courteous."
)

FIRST_SENTENCE = "Hello, namaste — I'm calling to confirm a contact detail, is this {name}'s number?"


# ── Identity judge: LLM primary ─────────────────────────────────────────────
_JUDGE_SYSTEM = (
    "You judge whether a phone call reached the intended doctor. "
    "Reply with ONLY a JSON object, no prose, no markdown fences. Schema: "
    '{"name_match": bool, "outcome": '
    '"confirmed"|"wrong"|"recovered"|"noanswer", '
    '"recovered_number": string, "confidence": number between 0 and 1}. '
    "outcome=confirmed when the respondent confirms it's the doctor or the "
    "doctor's clinic/office. outcome=wrong when it's clearly someone else. "
    "outcome=recovered when they give an alternate number. outcome=noanswer "
    "for voicemail/no pickup/empty transcript."
)


def _judge_llm(doctor: Doctor, transcript: str) -> dict | None:
    """Call a local Ollama model (default llama3.1) as the identity judge.

    Uses Ollama's /api/chat with format=json so the model is constrained to
    emit a single JSON object. No API key, no tokens billed — it runs on the
    machine Ollama is serving from. Returns None on any failure so the caller
    falls back to the rules matcher.
    """
    prompt = (
        f"Doctor name: {doctor.name}\n"
        f"Clinic address: {doctor.address}\n\n"
        f"Transcript:\n{transcript}"
    )
    try:
        resp = requests.post(
            f"{CONFIG.ollama_host}/api/chat",
            json={
                "model": CONFIG.ollama_model,
                "format": "json",              # force valid JSON output
                "stream": False,
                "options": {"temperature": 0},  # deterministic judging
                "messages": [
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=CONFIG.ollama_timeout,
        )
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "").strip()
        content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(content)
        data["_decided_by"] = "llm"
        return data
    except Exception:  # noqa: BLE001 — fall back to rules
        return None


# ── Identity judge: rules fallback ──────────────────────────────────────────
_WRONG_PAT = re.compile(
    r"\b(wrong number|galat number|galat hai|no doctor|koi doctor nahi|"
    r"nahi hai|not here|don'?t know|kaun|who is this)\b", re.I)
_CONFIRM_PAT = re.compile(
    r"\b(yes|haan|ha ji|speaking|bol raha|bol rahi|this is|clinic|hospital|"
    r"reception|doctor sahab|right number|sahi hai)\b", re.I)
_NUM_PAT = re.compile(r"(\+?91[\-\s]?\d{5}[\-\s]?\d{5}|\b[6-9]\d{4}[\-\s]?\d{5}\b)")
_CHANGED_PAT = re.compile(r"\b(changed|moved|shifted|naya number|new number|try|naya)\b", re.I)


def _judge_rules(doctor: Doctor, transcript: str) -> dict:
    t = transcript or ""
    if not t.strip():
        return {"name_match": False, "outcome": "noanswer", "recovered_number": "",
                "confidence": 0.5, "_decided_by": "rules"}
    last = doctor.name.replace("Dr.", "").replace("Dr", "").strip().split()
    name_hit = any(re.search(re.escape(p), t, re.I) for p in last if len(p) > 2)
    recovered = ""
    m = _NUM_PAT.search(t)
    if m:
        cand = m.group(0).replace(" ", "").replace("-", "")
        if cand not in doctor.phone.replace(" ", "").replace("-", ""):
            recovered = m.group(0).strip()

    # A fresh number + "changed/moved/try" means the line moved on — recovered,
    # even if the respondent was friendly. Check this before the confirm path.
    if recovered and _CHANGED_PAT.search(t):
        return {"name_match": False, "outcome": "recovered",
                "recovered_number": recovered, "confidence": 0.65, "_decided_by": "rules"}
    if _WRONG_PAT.search(t) and not name_hit:
        outcome = "recovered" if recovered else "wrong"
        return {"name_match": False, "outcome": outcome,
                "recovered_number": recovered, "confidence": 0.7, "_decided_by": "rules"}
    if _CONFIRM_PAT.search(t) or name_hit:
        return {"name_match": True, "outcome": "confirmed",
                "recovered_number": recovered, "confidence": 0.75 if name_hit else 0.6,
                "_decided_by": "rules"}
    return {"name_match": False, "outcome": "noanswer", "recovered_number": recovered,
            "confidence": 0.4, "_decided_by": "rules"}


def _user_utterances(transcript: str) -> list[str]:
    """Return the called party's spoken lines (those starting 'user:')."""
    out = []
    for line in (transcript or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("user:"):
            said = stripped[5:].strip()
            if said:
                out.append(said)
    return out


# Filler-only openers that don't constitute an answer on their own.
# NOTE: 'yes', 'haan', 'no', 'nahi' are NEVER filler — they're real answers.
_FILLER = {"hello", "hi", "hey", "hmm", "so", "um", "uh", "kaun", "ji", "acha",
           "oh", "arre", "sorry", "what", "pardon"}


def _has_substantive_answer(transcript: str) -> bool:
    """True if the callee said anything beyond filler.

    A short answer like 'Yes' or 'Haan ji' IS substantive — the old word-count
    guard wrongly treated those as no-answer. We only bail out when every user
    utterance is empty or pure filler (e.g. only 'Hello?' before the call drops).
    """
    utterances = _user_utterances(transcript)
    if not utterances:
        return False
    for u in utterances:
        cleaned = u.lower().strip(" .,!?")
        if cleaned and cleaned not in _FILLER:
            return True
    return False


def judge_identity(doctor: Doctor, transcript: str) -> dict:
    """LLM first, rules as fallback — but a call with no real answer is
    inconclusive and escalates to Phase 2.

    A SHORT answer ('Yes', 'Haan ji') is a real answer and is judged normally;
    only genuinely empty/filler-only calls (dropped after 'Hello?') bail out.
    """
    if not _has_substantive_answer(transcript):
        return {"name_match": False, "outcome": "noanswer",
                "recovered_number": "", "confidence": 0.3, "_decided_by": "guard"}
    if CONFIG.llm_live():
        res = _judge_llm(doctor, transcript)
        if res is not None:
            return res
    return _judge_rules(doctor, transcript)


# ── Bland telephony ─────────────────────────────────────────────────────────
def _place_call(doctor: Doctor, e164: str) -> str:
    # Only parameters documented on POST /v1/calls. Structured extraction is
    # handled by our own judge on the returned transcript, so no analysis_schema.
    body = {
        "phone_number": e164,
        "task": TASK_TEMPLATE.format(name=doctor.name),
        "first_sentence": FIRST_SENTENCE.format(name=doctor.name),
        "voice": CONFIG.bland_voice,
        "language": CONFIG.bland_language,   # e.g. "eng"; see .env note on Hindi
        "max_duration": CONFIG.bland_max_duration,
        "record": True,
        "wait_for_greeting": CONFIG.bland_wait_for_greeting,
        "noise_cancellation": True,
        "summary_prompt": (
            "In one line, state whether the call reached "
            f"{doctor.name} or their clinic, whether it was a wrong number, "
            "and any alternate phone number the person provided."
        ),
        "metadata": {"doctor_name": doctor.name},
    }
    # `from` must be a real Bland-purchased number; only send it if configured.
    if CONFIG.bland_from_number:
        body["from"] = CONFIG.bland_from_number

    resp = requests.post(
        f"{CONFIG.bland_base}/v1/calls",
        headers={"authorization": CONFIG.bland_api_key, "Content-Type": "application/json"},
        json=body, timeout=30,
    )
    # Surface Bland's own error list instead of a bare "400 Bad Request".
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:  # noqa: BLE001
            err = resp.text
        raise RuntimeError(f"Bland {resp.status_code}: {err}")
    data = resp.json()
    if data.get("status") != "success" or not data.get("call_id"):
        raise RuntimeError(f"Bland send failed: {data}")
    return data["call_id"]


def _poll_call(call_id: str) -> dict:
    deadline = time.time() + CONFIG.call_poll_timeout
    while time.time() < deadline:
        resp = requests.get(
            f"{CONFIG.bland_base}/v1/calls/{call_id}",
            headers={"authorization": CONFIG.bland_api_key}, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("completed") or data.get("queue_status") == "completed":
            return data
        time.sleep(CONFIG.call_poll_seconds)
    return {"completed": False, "concatenated_transcript": "", "summary": "poll timeout"}


def _mock_transcript(doctor: Doctor) -> tuple[str, str]:
    """Deterministic fake transcript covering the four outcomes."""
    h = int(hashlib.sha256((doctor.phone + "p1").encode()).hexdigest(), 16)
    r = (h % 100) / 100.0
    if r < 0.45:
        return (f"assistant: Namaste, is this {doctor.name}'s clinic?\n"
                f"user: Haan ji, yes this is {doctor.name} speaking.", "Confirmed doctor's clinic.")
    if r < 0.60:
        return ("assistant: Hello, am I reaching the doctor?\n"
                "user: Wrong number, koi doctor nahi hai yahan.", "Wrong number.")
    if r < 0.72:
        return ("assistant: Looking for the doctor.\n"
                "user: They changed clinic, try 98765 43210.", "Alternate number given.")
    return ("", "No answer / voicemail.")


def _run_one_call(doctor: Doctor, e164: str, live: bool) -> Phase1Result:
    """A single call attempt (or one mock outcome), judged."""
    call_id = None
    recording = None
    if live:
        try:
            call_id = _place_call(doctor, e164)
            details = _poll_call(call_id)
            transcript = details.get("concatenated_transcript", "") or ""
            summary = details.get("summary", "") or ""
            recording = details.get("recording_url")
        except Exception as exc:  # noqa: BLE001
            return Phase1Result(outcome="error", name_match=False, confidence=0.0,
                                transcript="", summary=f"call error: {exc}",
                                call_id=call_id, decided_by="", live_call=True)
    else:
        transcript, summary = _mock_transcript(doctor)

    j = judge_identity(doctor, transcript)
    return Phase1Result(
        outcome=j.get("outcome", "noanswer"),
        name_match=bool(j.get("name_match", False)),
        confidence=float(j.get("confidence", 0.0)),
        transcript=transcript,
        summary=summary,
        recovered_number=(j.get("recovered_number") or None),
        recording_url=recording,
        call_id=call_id,
        decided_by=j.get("_decided_by", ""),
        live_call=live,
    )


def run_phase1(doctor: Doctor, e164: str) -> Phase1Result:
    """Place the call, judge it, and re-dial ONCE if the first try was a
    no-answer / dropped / too-short call (a transient failure worth retrying).

    A 'wrong' or 'confirmed' result is a real answer — never retried. Retry is
    skipped in mock mode and can be turned off with BLAND_RETRY_ON_NOANSWER.
    """
    live = CONFIG.phase1_live()
    result = _run_one_call(doctor, e164, live)

    retryable = result.outcome in {"noanswer", "error"}
    if retryable and live and CONFIG.bland_retry_on_noanswer:
        log.info("   ↻ no answer — re-dialing once in %ds", CONFIG.bland_retry_wait)
        time.sleep(CONFIG.bland_retry_wait)
        second = _run_one_call(doctor, e164, live)
        # Keep the better result: any real contact beats a second no-answer.
        if second.outcome not in {"noanswer", "error"}:
            second.summary = (second.summary + " (on 2nd attempt)").strip()
            return second
        # Both failed — return whichever has more signal (a transcript).
        chosen = second if second.transcript else result
        chosen.summary = chosen.summary or "no answer after 2 attempts"
        return chosen

    return result
