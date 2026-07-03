"""Phase 0 — Pre-screen.

Two cheap checks before we ever dial:
  1. Format / region: normalize to E.164, reject landlines & junk, derive circle.
  2. HLR lookup (MSG91): is the number live, ported, or disconnected at carrier?

This is the highest-ROI phase: it kills dead numbers for a fraction of a
rupee each, so we never burn call budget on them.
"""
from __future__ import annotations

import hashlib

import requests

from .config import CONFIG
from .models import Doctor, Phase0Result

# Indian telecom circle inferred from address keywords (best-effort, for scoring).
_CIRCLE_KEYWORDS = {
    "punjab": "Punjab", "mumbai": "Maharashtra", "maharashtra": "Maharashtra",
    "pune": "Maharashtra", "bengaluru": "Karnataka", "bangalore": "Karnataka",
    "karnataka": "Karnataka", "chennai": "Tamil Nadu", "tamil nadu": "Tamil Nadu",
    "hyderabad": "AP/Telangana", "telangana": "AP/Telangana", "lucknow": "UP",
    "uttar pradesh": "UP", "kolkata": "Kolkata/WB", "west bengal": "Kolkata/WB",
    "delhi": "Delhi", "kochi": "Kerala", "kerala": "Kerala",
}


def normalize_e164(phone: str) -> tuple[bool, str, str]:
    """Return (is_valid_mobile, e164, reason). India-centric.

    Uses the `phonenumbers` library if installed for correctness; falls back
    to a simple +91 heuristic otherwise.
    """
    try:
        import phonenumbers

        num = phonenumbers.parse(phone, "IN")
        if not phonenumbers.is_valid_number(num):
            return False, "", "invalid number per libphonenumber"
        ntype = phonenumbers.number_type(num)
        mobile_types = {
            phonenumbers.PhoneNumberType.MOBILE,
            phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE,
        }
        e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
        if ntype not in mobile_types:
            return False, e164, "not a mobile line"
        return True, e164, "valid mobile"
    except ImportError:
        digits = "".join(c for c in phone if c.isdigit())
        if digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == 10:
            digits = "91" + digits
        if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
            return True, "+" + digits, "valid mobile (heuristic)"
        return False, "+" + digits if digits else "", "invalid format (heuristic)"


def _circle_from_address(address: str) -> str:
    a = address.lower()
    for k, v in _CIRCLE_KEYWORDS.items():
        if k in a:
            return v
    return "Unknown"


def _mock_hlr(e164: str) -> tuple[str, bool]:
    """Deterministic mock so the same number always returns the same result."""
    h = int(hashlib.sha256(e164.encode()).hexdigest(), 16)
    r = (h % 100) / 100.0
    ported = ((h >> 8) % 100) / 100.0 > 0.8
    status = "dead" if r < 0.25 else "live"
    return status, ported


def _live_hlr(e164: str) -> tuple[str, bool, str]:
    """Neutrino HLR lookup. Returns (status, ported, raw_hlr_status).

    Docs: https://www.neutrinoapi.com/api/hlr-lookup/
    Auth: User-ID + API-Key headers. Request is form-urlencoded.
    Neutrino only bills for actual mobile lookups — it validates the number
    type first, so landlines/VOIP don't cost an HLR credit.

    hlr-status values: ok | absent | unknown | invalid | fixed-line | voip |
    failed. Only "ok" means a live, reachable device.
    """
    resp = requests.post(
        CONFIG.neutrino_hlr_url,
        headers={
            "User-ID": CONFIG.neutrino_user_id,
            "API-Key": CONFIG.neutrino_api_key,
        },
        data={  # form-urlencoded, not JSON
            "number": e164,
            "country-code": CONFIG.neutrino_country_code,
        },
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    hlr_status = str(data.get("hlr-status", "")).lower()
    ported = bool(data.get("is-ported", False))
    # "ok" = connected/registered device. Everything else is not reachable.
    return ("live" if hlr_status == "ok" else "dead"), ported, (hlr_status or "unknown")


def run_phase0(doctor: Doctor) -> Phase0Result:
    is_mobile, e164, reason = normalize_e164(doctor.phone)
    circle = _circle_from_address(doctor.address)

    if not is_mobile:
        return Phase0Result(
            passed=False, line_status="invalid", reason=reason,
            ported=None, circle=circle, e164=e164 or None, live_lookup=False,
        )

    if CONFIG.phase0_live():
        try:
            status, ported, raw = _live_hlr(e164)
            live_lookup = True
            reason = f"Neutrino HLR: {raw}"
        except Exception as exc:  # noqa: BLE001 — degrade gracefully, never crash a batch
            status, ported = "live", None
            live_lookup = False
            reason = f"HLR error ({exc}); passed on format only"
    elif CONFIG.mock:
        status, ported = _mock_hlr(e164)
        live_lookup = False
        reason = "HLR (mock): " + status
    else:
        # Live run but no HLR provider set → format validation only.
        status, ported = "live", None
        live_lookup = False
        reason = "format valid (no HLR provider configured)"

    return Phase0Result(
        passed=(status == "live"),
        line_status=status,
        reason=reason,
        ported=ported,
        circle=circle,
        e164=e164,
        live_lookup=live_lookup,
    )
