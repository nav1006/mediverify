"""Configuration loaded from environment variables.

Copy .env.example to .env and fill in your keys. Nothing here is hardcoded;
if a key is missing, the matching provider runs in MOCK mode so the whole
pipeline is testable end-to-end without spending a paisa.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional
    pass


def _b(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    # ── Phase 0: pre-screen (HLR) — Neutrino API ───────────────────────
    # https://www.neutrinoapi.com/api/hlr-lookup/  — needs a verified account.
    # Auth is two headers: User-ID + API-Key. Leave blank to run Phase 0 as
    # format-only validation (no carrier lookup).
    neutrino_user_id: str = field(default_factory=lambda: os.getenv("NEUTRINO_USER_ID", ""))
    neutrino_api_key: str = field(default_factory=lambda: os.getenv("NEUTRINO_API_KEY", ""))
    neutrino_hlr_url: str = "https://neutrinoapi.net/hlr-lookup"
    neutrino_country_code: str = field(default_factory=lambda: os.getenv("NEUTRINO_COUNTRY_CODE", "IN"))

    # ── Phase 1: voice — Bland.ai ──────────────────────────────────────
    bland_api_key: str = field(default_factory=lambda: os.getenv("BLAND_API_KEY", ""))
    bland_base: str = "https://api.bland.ai"
    bland_from_number: str = field(default_factory=lambda: os.getenv("BLAND_FROM_NUMBER", ""))
    bland_voice: str = field(default_factory=lambda: os.getenv("BLAND_VOICE", "maya"))
    # Bland uses ISO-style codes like "eng", "hin". "babel" is NOT valid and 400s.
    # For Hindi/English mixing, "hin" tends to handle code-switching best.
    bland_language: str = field(default_factory=lambda: os.getenv("BLAND_LANGUAGE", "eng"))
    bland_max_duration: int = int(os.getenv("BLAND_MAX_DURATION", "5"))  # minutes
    # Wait for the callee to say hello before the agent speaks — avoids the
    # agent talking over "Hello?" and cutting the call short.
    bland_wait_for_greeting: bool = field(
        default_factory=lambda: os.getenv("BLAND_WAIT_FOR_GREETING", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    # Seconds Bland waits for that greeting before the agent proceeds anyway.
    bland_greeting_timeout: int = int(os.getenv("BLAND_GREETING_TIMEOUT", "7"))
    # Auto re-dial once when a call comes back as no-answer / too short.
    bland_retry_on_noanswer: bool = field(
        default_factory=lambda: os.getenv("BLAND_RETRY_ON_NOANSWER", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    bland_retry_wait: int = int(os.getenv("BLAND_RETRY_WAIT", "20"))  # seconds between tries
    call_poll_seconds: int = int(os.getenv("CALL_POLL_SECONDS", "15"))
    call_poll_timeout: int = int(os.getenv("CALL_POLL_TIMEOUT", "300"))

    # ── Phase 1 brain: LLM identity judge — local Ollama ───────────────
    ollama_host: str = field(default_factory=lambda: os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "llama3:8b"))
    ollama_timeout: int = int(os.getenv("OLLAMA_TIMEOUT", "60"))

    # ── Phase 2: fallback — MSG91 WhatsApp / SMS ───────────────────────
    msg91_authkey: str = field(default_factory=lambda: os.getenv("MSG91_AUTHKEY", ""))
    msg91_whatsapp_url: str = "https://api.msg91.com/api/v5/whatsapp/whatsapp-outbound-message/bulk/"
    msg91_wa_integrated_number: str = field(
        default_factory=lambda: os.getenv("MSG91_WA_NUMBER", "")
    )
    msg91_wa_template: str = field(default_factory=lambda: os.getenv("MSG91_WA_TEMPLATE", ""))
    msg91_wa_lang: str = field(default_factory=lambda: os.getenv("MSG91_WA_LANG", "en"))

    # ── Run behaviour ──────────────────────────────────────────────────
    # MOCK forces every provider to simulate, even if keys are present.
    mock: bool = field(default_factory=lambda: _b("MOCK", False))
    max_workers: int = int(os.getenv("MAX_WORKERS", "1"))  # keep 1 for live calls
    verified_threshold: int = int(os.getenv("VERIFIED_THRESHOLD", "60"))
    review_threshold: int = int(os.getenv("REVIEW_THRESHOLD", "30"))

    def phase0_live(self) -> bool:
        return not self.mock and bool(self.neutrino_user_id) and bool(self.neutrino_api_key)

    def phase1_live(self) -> bool:
        return not self.mock and bool(self.bland_api_key)

    def llm_live(self) -> bool:
        """True if a local Ollama server is reachable. Probed once, then cached.

        In MOCK mode we skip the probe entirely so demos never touch the network.
        """
        if self.mock:
            return False
        if getattr(self, "_ollama_ok", None) is None:
            try:
                import requests

                r = requests.get(f"{self.ollama_host}/api/tags", timeout=2)
                object.__setattr__(self, "_ollama_ok", r.status_code == 200)
            except Exception:  # noqa: BLE001
                object.__setattr__(self, "_ollama_ok", False)
        return bool(self._ollama_ok)

    def phase2_live(self) -> bool:
        return not self.mock and bool(self.msg91_authkey) and bool(self.msg91_wa_integrated_number)


CONFIG = Config()
