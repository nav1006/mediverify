"""Phase 2 — Fallback (MSG91 WhatsApp, SMS as last resort).

Only runs when Phase 1 was inconclusive (no answer / voicemail / recovered),
because plenty of doctors won't pick up an unknown number. A WhatsApp
business-profile name is a strong identity signal; a read receipt is at least
a reachability signal.

Note: this prototype only *sends* the outbound message. Capturing the reply /
profile name requires MSG91's inbound webhook, which belongs in the always-on
service version, not this batch script — so live mode here records "delivered"
and leaves name_match to the webhook layer.
"""
from __future__ import annotations

import hashlib

import requests

from .config import CONFIG
from .models import Doctor, Phase2Result


def _mock_fallback(doctor: Doctor) -> Phase2Result:
    h = int(hashlib.sha256((doctor.phone + "p2").encode()).hexdigest(), 16)
    r = (h % 100) / 100.0
    if r < 0.40:
        return Phase2Result(channel="WhatsApp", result="profile-match",
                            detail=f'WhatsApp profile reads "{doctor.name}"',
                            name_match=True, live_send=False)
    if r < 0.65:
        return Phase2Result(channel="WhatsApp", result="delivered",
                            detail="Delivered + read, no profile name",
                            name_match=False, live_send=False)
    return Phase2Result(channel="SMS", result="no-response",
                        detail="SMS delivered, no reply in window",
                        name_match=False, live_send=False)


def _send_whatsapp(doctor: Doctor, e164: str) -> Phase2Result:
    # Structure per MSG91 docs: messaging_product + to_and_components.
    payload = {
        "integrated_number": CONFIG.msg91_wa_integrated_number,
        "content_type": "template",
        "payload": {
            "messaging_product": "whatsapp",
            "type": "template",
            "template": {
                "name": CONFIG.msg91_wa_template,
                "language": {"code": CONFIG.msg91_wa_lang, "policy": "deterministic"},
                "to_and_components": [
                    {"to": [e164.lstrip("+")], "components": {}}
                ],
            },
        },
    }
    resp = requests.post(
        CONFIG.msg91_whatsapp_url,
        headers={"authkey": CONFIG.msg91_authkey, "Content-Type": "application/json",
                 "accept": "application/json"},
        json=payload, timeout=20,
    )
    if resp.status_code >= 400:
        try:
            err = resp.json()
        except Exception:  # noqa: BLE001
            err = resp.text
        raise RuntimeError(f"MSG91 {resp.status_code}: {err}")
    return Phase2Result(channel="WhatsApp", result="delivered",
                        detail="Template sent; awaiting inbound webhook for reply/profile",
                        name_match=False, live_send=True)


def run_phase2(doctor: Doctor, e164: str) -> Phase2Result:
    if CONFIG.phase2_live():
        try:
            return _send_whatsapp(doctor, e164)
        except Exception as exc:  # noqa: BLE001
            return Phase2Result(channel="WhatsApp", result="error",
                                detail=f"send error: {exc}", name_match=False, live_send=True)
    return _mock_fallback(doctor)
