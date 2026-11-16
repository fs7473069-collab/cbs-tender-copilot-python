"""Deterministic reviewer actions from evidence-match status.

Snapshot-only. Not a human decision, not a completed task, and not sent on
RabbitMQ analysis messages. Does not call Ollama.
"""

SUGGESTED_STATUS = "AI_SUGGESTED"
_ACTION_ID_PREFIX = "AI-SUGGESTED-"

_VERIFY_TITLE = "AI suggested: Verify retrieved CBS evidence"
_COLLECT_TITLE = "AI suggested: Identify CBS evidence"
_RETRY_TITLE = "AI suggested: Investigate evidence matching"


def _requirement_id(requirement):
    for key in ("requirementId", "sourceItemKey"):
        value = requirement.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _evidence_status(evidence_match):
    if not isinstance(evidence_match, dict):
        return None
    status = evidence_match.get("status")
    if not isinstance(status, str) or not status.strip():
        return None
    return status.strip().upper()


def _citations(evidence_match):
    citations = []
    seen = set()
    for item in evidence_match.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        source_text = source.strip() if isinstance(source, str) else ""
        page = item.get("page")
        if not source_text and page is None:
            continue
        key = (source_text, page)
        if key in seen:
            continue
        seen.add(key)
        if source_text and page is not None:
            citations.append(f"{source_text}, p.{page}")
        elif source_text:
            citations.append(source_text)
        else:
            citations.append(f"p.{page}")
    return citations


def _suggested_action(requirement_id, title, description):
    return {
        "actionId": f"{_ACTION_ID_PREFIX}{requirement_id}",
        "requirementId": requirement_id,
        "title": title,
        "description": description,
        "status": SUGGESTED_STATUS,
    }


def action_from_requirement(requirement):
    """Return one suggested action dict, or ``None``."""
    requirement_id = _requirement_id(requirement)
    if requirement_id is None:
        return None
    evidence_match = requirement.get("evidenceMatch")
    status = _evidence_status(evidence_match)
    if status == "REVIEW_REQUIRED":
        citations = _citations(evidence_match)
        cited = (
            " Cited source: " + "; ".join(citations) + "."
            if citations else ""
        )
        return _suggested_action(
            requirement_id,
            _VERIFY_TITLE,
            "Verify the retrieved CBS evidence against this requirement."
            f"{cited} AI-suggested only; not a completed task or human decision.",
        )
    if status == "NOT_FOUND":
        if requirement.get("mandatory") is not True:
            return None
        return _suggested_action(
            requirement_id,
            _COLLECT_TITLE,
            "No acceptable CBS evidence was found. Identify, collect, or "
            "prepare CBS evidence for this mandatory requirement. "
            "AI-suggested only; not a completed task or human decision.",
        )
    if status == "NOT_CHECKED":
        return _suggested_action(
            requirement_id,
            _RETRY_TITLE,
            "Evidence matching could not be completed. Retry or investigate "
            "the matching failure. This does not mean evidence is missing. "
            "AI-suggested only; not a completed task or human decision.",
        )
    return None


def merge_suggested_actions(requirements, existing_actions=None):
    """Keep existing actions; append at most one suggestion per requirement."""
    merged = []
    claimed_ids = set()
    claimed_requirements = set()
    for item in existing_actions or []:
        if not isinstance(item, dict):
            continue
        copied = dict(item)
        merged.append(copied)
        action_id = copied.get("actionId")
        if isinstance(action_id, str) and action_id.strip():
            claimed_ids.add(action_id.strip())
        req_id = copied.get("requirementId")
        if isinstance(req_id, str) and req_id.strip():
            claimed_requirements.add(req_id.strip())

    for requirement in requirements or []:
        if not isinstance(requirement, dict):
            continue
        suggested = action_from_requirement(requirement)
        if suggested is None:
            continue
        req_id = suggested["requirementId"]
        action_id = suggested["actionId"]
        if req_id in claimed_requirements or action_id in claimed_ids:
            continue
        merged.append(suggested)
        claimed_ids.add(action_id)
        claimed_requirements.add(req_id)
    return merged
