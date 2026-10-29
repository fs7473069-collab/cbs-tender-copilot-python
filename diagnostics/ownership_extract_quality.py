"""Strict post-hoc quality checks for ownership-table extraction diagnostics.

Does not call Ollama or change production extraction. Used by the ownership
chunk benchmark and offline unit tests.
"""

from __future__ import annotations

import re

# Human-readable labels for failure reporting (must match fixture ownership).
PRICING_ASSIGNMENT_LABELS = {
    "option_a_qualiteq": "Option A: Qualiteq",
    "option_b_vendor": "Option B: Vendor",
    "option_c_vendor": "Option C (Full Stack): Vendor",
}

_PRICING_ROW_RE = re.compile(
    r"pricing\s*(?:&|and)?\s*product\s*engine",
    re.IGNORECASE,
)

# Pair owner with option locally — do not treat "all options" as Option C.
_OPTION_A_QUALITEQ = re.compile(
    r"(?:"
    r"qualiteq.{0,80}option\s*a"
    r"|"
    r"option\s*a.{0,80}qualiteq"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_OPTION_B_VENDOR = re.compile(
    r"(?:"
    r"vendor.{0,80}option\s*b"
    r"|"
    r"option\s*b.{0,80}vendor"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_OPTION_C_VENDOR = re.compile(
    r"(?:"
    r"vendor.{0,80}(?:option\s*c|full\s*stack)"
    r"|"
    r"(?:option\s*c|full\s*stack).{0,80}vendor"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def find_pricing_engine_requirement(requirements):
    """Return the requirement dict that describes the pricing & product engine row."""
    if not isinstance(requirements, list):
        return None
    for item in requirements:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and _PRICING_ROW_RE.search(text):
            return item
    return None


def _assignment_presence(text: str) -> dict:
    return {
        "option_a_qualiteq": bool(_OPTION_A_QUALITEQ.search(text)),
        "option_b_vendor": bool(_OPTION_B_VENDOR.search(text)),
        "option_c_vendor": bool(_OPTION_C_VENDOR.search(text)),
    }


def check_pricing_engine_ownership(requirements):
    """Verify Pricing & product engine preserves A/B/C ownership from the fixture.

    Required assignments (fixture):
      - Option A: Qualiteq
      - Option B: Vendor
      - Option C (Full Stack): Vendor

    Generic mentions of Option A/B/C elsewhere, or the phrase \"all options\",
    do not satisfy this check. Missing assignments are listed explicitly.
    """
    pricing = find_pricing_engine_requirement(requirements)
    if pricing is None:
        missing = list(PRICING_ASSIGNMENT_LABELS.values())
        return {
            "passed": False,
            "component": "Pricing & product engine",
            "matched_requirement_text": None,
            "assignments": {
                key: {
                    "required": label,
                    "present": False,
                }
                for key, label in PRICING_ASSIGNMENT_LABELS.items()
            },
            "missing_assignments": missing,
            "note": (
                "No extracted requirement matches the Pricing & product engine "
                "row; generic Option A/B/C or 'all options' flags are ignored."
            ),
        }

    text = pricing.get("text") or ""
    presence = _assignment_presence(text)
    assignments = {}
    missing = []
    for key, label in PRICING_ASSIGNMENT_LABELS.items():
        present = presence[key]
        assignments[key] = {"required": label, "present": present}
        if not present:
            missing.append(label)

    return {
        "passed": len(missing) == 0,
        "component": "Pricing & product engine",
        "matched_requirement_text": text,
        "assignments": assignments,
        "missing_assignments": missing,
        "note": (
            None
            if not missing
            else (
                "Pricing ownership incomplete; missing: "
                + "; ".join(missing)
            )
        ),
    }
