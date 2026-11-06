"""Validation and normalisation of the export snapshot sent by Java.

The snapshot is the only source of export content. Nothing here derives,
infers or fills in business data: missing values stay missing.
"""

APPROVED = "APPROVED"
# The only human compliance decisions (PROPOSED; Java to confirm). Matching is
# trimmed and case-insensitive; anything else (missing, null, PENDING, TBD,
# N/A, UNKNOWN, "PARTIALLY COMPLIANT", ...) means no decision.
DECISION_VALUES = frozenset({
    "COMPLIANT",
    "PARTIALLY_COMPLIANT",
    "NON_COMPLIANT",
    "NOT_APPLICABLE",
})


class SnapshotError(ValueError):
    """The snapshot structure is invalid; retrying will not help."""


def _optional_list_of_dicts(container, field, where):
    value = container.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise SnapshotError(f"{where}.{field} must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SnapshotError(f"{where}.{field}[{index}] must be an object")
    return list(value)


def _optional_dict(container, field, where):
    value = container.get(field)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SnapshotError(f"{where}.{field} must be an object or null")
    return value


def _is_blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _normalize_requirement(requirement, index):
    where = f"snapshot.requirements[{index}]"
    content = requirement.get("content")
    if not isinstance(content, str) or not content.strip():
        raise SnapshotError(f"{where}.content must be a non-empty string")
    if _is_blank(requirement.get("requirementId")) and _is_blank(
        requirement.get("sourceItemKey")
    ):
        raise SnapshotError(
            f"{where} needs a requirementId or sourceItemKey"
        )
    mandatory = requirement.get("mandatory")
    if mandatory is not None and not isinstance(mandatory, bool):
        raise SnapshotError(f"{where}.mandatory must be a boolean or null")

    normalized = dict(requirement)
    normalized["sources"] = _optional_list_of_dicts(requirement, "sources", where)
    evidence_match = _optional_dict(requirement, "evidenceMatch", where)
    if evidence_match is not None:
        evidence_match = dict(evidence_match)
        evidence_match["evidence"] = _optional_list_of_dicts(
            evidence_match, "evidence", where + ".evidenceMatch"
        )
    normalized["evidenceMatch"] = evidence_match
    normalized["decision"] = _optional_dict(requirement, "decision", where)
    recommendation = requirement.get("aiRecommendation")
    if recommendation is not None and not isinstance(recommendation, (str, dict)):
        raise SnapshotError(
            f"{where}.aiRecommendation must be a string, object or null"
        )
    return normalized


def normalize_snapshot(snapshot):
    """Return a validated copy with list fields defaulted to empty lists."""
    if not isinstance(snapshot, dict):
        raise SnapshotError("snapshot must be an object")
    requirements = snapshot.get("requirements")
    if not isinstance(requirements, list):
        raise SnapshotError("snapshot.requirements must be a list")
    for index, item in enumerate(requirements):
        if not isinstance(item, dict):
            raise SnapshotError(
                f"snapshot.requirements[{index}] must be an object"
            )

    normalized = dict(snapshot)
    normalized["tender"] = _optional_dict(snapshot, "tender", "snapshot") or {}
    normalized["requirements"] = [
        _normalize_requirement(item, index)
        for index, item in enumerate(requirements)
    ]
    normalized["actions"] = _optional_list_of_dicts(snapshot, "actions", "snapshot")
    normalized["draftSections"] = _optional_list_of_dicts(
        snapshot, "draftSections", "snapshot"
    )
    for index, section in enumerate(normalized["draftSections"]):
        order = section.get("order")
        if order is not None and (
            isinstance(order, bool) or not isinstance(order, (int, float))
        ):
            raise SnapshotError(
                f"snapshot.draftSections[{index}].order must be a number or null"
            )
    return normalized


def decision_status(requirement):
    """The canonical human decision (one of DECISION_VALUES) or None.

    Never derived from evidence status or AI recommendation.
    """
    decision = requirement.get("decision")
    if not isinstance(decision, dict):
        return None
    status = decision.get("status")
    if not isinstance(status, str):
        return None
    canonical = status.strip().upper()
    return canonical if canonical in DECISION_VALUES else None


def has_decision(requirement):
    return decision_status(requirement) is not None


def count_mandatory_without_decision(snapshot):
    normalized = normalize_snapshot(snapshot)
    return sum(
        1
        for requirement in normalized["requirements"]
        if requirement.get("mandatory") is True and not has_decision(requirement)
    )


def is_approved_section(section):
    """Only an explicit approvalStatus of APPROVED (any case) qualifies."""
    status = section.get("approvalStatus") if isinstance(section, dict) else None
    return isinstance(status, str) and status.strip().upper() == APPROVED
