# MediVerify — local doctor phone-verification agent

A command-line Python agent that takes a CSV of doctors (`name, phone, address`)
and runs each one through a three-phase verification pipeline to answer two
questions: **is the number live**, and **does it reach the right doctor**.

No web interface. No database integration yet (Zoho is the next phase). Just a
script you run locally that reads a CSV and writes results.

## The pipeline

```
CSV ─► Phase 0: PRE-SCREEN ──► Phase 1: AI VOICE CALL ──► Phase 2: FALLBACK ──► score + verdict
       format + HLR lookup     identity confirmation       WhatsApp / SMS
       (MSG91)                  (Bland.ai + LLM judge)      (MSG91)
```

**Escalation is the whole point** — a number only moves to the next phase when
the prior one is inconclusive:

- **Phase 0** normalizes to E.164, drops landlines/junk, and does an HLR lookup
  (live/ported/disconnected). Dead numbers stop here and are **never dialed** —
  this is where most of the cost saving comes from.
- **Phase 1** places an AI voice call via Bland.ai (handles Hindi/English via
  Bland's `babel` mode), pulls the transcript, and decides identity with a
  **two-tier judge**: an LLM reads the transcript and returns strict JSON; if
  the LLM is unavailable or returns garbage, a keyword/rule matcher takes over.
  Outcomes: `confirmed` / `wrong` / `recovered` (alternate number given) /
  `noanswer`.
- **Phase 2** runs only when Phase 1 is inconclusive (no answer / recovered).
  Sends a WhatsApp template (a matching business-profile name is a strong
  identity signal), SMS as last resort.

A transparent weighted **score (0–100)** combines the signals into a verdict:
`verified` / `needs_review` / `wrong_number` / `dead_unverified`.

## Why these tools

| Phase | Tool | Why |
|---|---|---|
| 0 HLR | **Neutrino API** | cheap carrier lookup (~$0.01/number); kills dead numbers pre-call. Only bills for real mobile lookups. (MSG91 has no HLR API.) |
| 1 call | **Bland.ai** | places the call *and* runs the conversation; `babel` handles Hindi/English code-switching; one API to send + poll transcript |
| 1 judge | **Ollama (llama3:8b)** + rules | local LLM reads messy bilingual transcripts, no tokens billed; rules are a no-network fallback |
| 2 fallback | **MSG91** | same vendor for WhatsApp + SMS |

> Bland was chosen over Vapi for Phase 1 because the job is "make a call, get a
> structured transcript back" — Bland does that in a single send+poll flow.
> Swap it freely: the identity judge in `phase1_voice.py` is decoupled from the
> telephony, so changing dialer doesn't touch the decision logic.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in keys; any blank key => that phase mocks
```

### Phase 1 brain — Ollama (local, free)

The identity judge runs on a **local Ollama model** (`llama3:8b` by default), so
no tokens are billed and transcripts never leave your machine. Set it up once:

```bash
# install Ollama from https://ollama.com, then:
ollama pull llama3:8b
ollama serve            # usually already running as a background service
```

The agent auto-detects whether Ollama is reachable at `OLLAMA_HOST`. If it's
down, Phase 1 silently falls back to the keyword/rules judge — the pipeline
never breaks. Point `OLLAMA_MODEL` at any model you've pulled (e.g. `llama3.1`,
`qwen2.5`).

## Run

### Single number — demo mode (show this to your boss)

Give it one phone number **with country code** and watch it walk the phases:

```bash
# One number, with the name to verify against
python -m mediverify.cli --mock check +919876543210 --name "Dr. Ananya Sharma"

# Add an address so Phase 0 can match the telecom circle
python -m mediverify.cli --mock check +919876543210 --name "Dr. A" --address "Ludhiana, Punjab"

# Interactive loop — keep typing numbers without restarting
python -m mediverify.cli --mock check --repeat

# No number at all → it prompts you for one
python -m mediverify.cli --mock check

# Structured output instead of the pretty view — the exact shape that
# will feed Zoho next phase. Pipe it anywhere (e.g. | jq).
python -m mediverify.cli --mock check +919876543210 --name "Dr. A" --json
```

Drop `--mock` to run against real providers (whichever keys are set in `.env`).
Output is printed phase-by-phase with the final score and verdict.

### Batch mode — CSV

```bash
# Mock mode — no API calls, no keys needed.
python -m mediverify.cli verify sample_doctors.csv --out runs/demo --mock

# Live — uses whichever providers have keys set in .env
python -m mediverify.cli verify your_doctors.csv --out runs/2026-06-30

# First 20 rows only
python -m mediverify.cli verify your_doctors.csv --limit 20
```

### Per-phase live/mock is independent

Each phase checks its own keys. Set only `MSG91_AUTHKEY` and Phase 0 goes live
while Phases 1–2 stay mocked. This lets you bring the pipeline up one provider
at a time and keep testing the rest for free.

## Outputs (written to `--out`)

- **`results.csv`** — one row per doctor, every field flattened.
- **`results.json`** — full detail including call transcripts and recording URLs.
- **`sales_shortlist.csv`** — only `verified` + `needs_review`, with the best
  number (recovered number preferred). This is the pre-qualified list your sales
  team actually calls — the 21k → few-thousand reduction.

## Files

```
mediverify/
  config.py            env-driven settings; decides live vs mock per phase
  models.py            dataclasses passed between phases
  phase0_prescreen.py  E.164 normalize + Neutrino HLR
  phase1_voice.py      Bland call + LLM/rules identity judge
  phase2_fallback.py   MSG91 WhatsApp/SMS
  scoring.py           weighted score + verdict
  agent.py             orchestration, CSV I/O, escalation
  cli.py               command-line entrypoint
sample_doctors.csv     10 rows incl. a landline + a junk number to show Phase 0
.env.example
requirements.txt
```

## Honest limitations (for the demo)

- **Mock mode fabricates outcomes** via a seeded hash so runs are reproducible.
  Nothing is dialed.
- **Phase 2 only sends** the outbound message in live mode. Capturing the reply
  / WhatsApp profile name needs MSG91's *inbound webhook*, which belongs in the
  always-on service version, not this batch script.
- **HLR provider**: Phase 0 uses the Neutrino API (MSG91 has no HLR
  endpoint). Set `NEUTRINO_USER_ID` + `NEUTRINO_API_KEY` from your Neutrino
  dashboard. Leave them blank to run Phase 0 as format-only validation — it
  still filters landlines and malformed numbers, just without a live carrier
  check. Neutrino returns a precise `hlr-status` (ok / absent / fixed-line /
  voip / …); only `ok` counts as a live, reachable device.
- **Bland gotchas**: `language` must be a valid code like `eng`/`hin` (not
  `babel`); `from` must be a real Bland-purchased number or omitted; the send
  fails with a 400 listing the exact bad field, which the agent now surfaces.
- **Compliance**: live automated calls/SMS in India are subject to TRAI DLT and
  DND rules. Keep the script clearly non-promotional and route through a
  DLT-registered sender before going to volume.

## Next phase

Zoho CRM write-back (via its MCP): push `verdict`, `score`, `recovered_number`,
`recording_url`, `last_attempt` per record, and auto-route the shortlist to
sales. The `VerificationRecord.to_row()` / `to_json()` methods already produce
the field set you'd map to Zoho.
