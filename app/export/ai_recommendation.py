"""Deterministic Excel AI recommendations from evidence-match status.

This is not a compliance decision. It does not call Ollama. Unknown or missing
statuses produce no recommendation.
"""

# Evidence matching has already finished (including KB catalog filtering)
# before the analysis snapshot is built.
_FROM_EVIDENCE_STATUS = {
    "REVIEW_REQUIRED": {
        "recommendation": "NEEDS_HUMAN_REVIEW",
        "rationale": (
            "Retrieved evidence requires reviewer verification. "
            "It does not prove compliance."
        ),
    },
    "NOT_FOUND": {
        "recommendation": "EVIDENCE_REQUIRED",
        "rationale": "No acceptable CBS evidence was found.",
    },
    "NOT_CHECKED": {
        "recommendation": "NOT_CHECKED",
        "rationale": "Evidence matching could not be completed.",
    },
}


def recommendation_from_evidence_match(evidence_match):
    """Return ``{recommendation, rationale}`` or ``None``.

    ``None`` means do not invent a recommendation (missing match, blank
    status, or an unrecognised status).
    """
    if not isinstance(evidence_match, dict):
        return None
    status = evidence_match.get("status")
    if not isinstance(status, str) or not status.strip():
        return None
    mapped = _FROM_EVIDENCE_STATUS.get(status.strip().upper())
    if mapped is None:
        return None
    return {
        "recommendation": mapped["recommendation"],
        "rationale": mapped["rationale"],
    }
