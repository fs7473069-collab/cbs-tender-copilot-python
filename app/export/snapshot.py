"""Validation and normalisation of the export snapshot sent by Java.

The snapshot is the only source of export content. Nothing here derives,
infers or fills in business data: missing values stay missing.
"""

APPROVED = "APPROVED"
DRAFT_PENDING_REVIEW_LABEL = "DRAFT — PENDING REVIEW"
MISSING_DRAFT_CONTENT = (
    "No generated content is available for this section. "
    "This export does not invent a bid response."
)
MISSING_DRAFT_SECTION = (
    "This section was not generated. The model output was missing, "
    "truncated, or failed validation. This export does not invent a bid response."
)
NO_DRAFT_SECTIONS = (
    "No AI draft sections are present in this snapshot. "
    "This export does not invent bid-response content."
)
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
    normalized["recommendations"] = _optional_list_of_dicts(
        snapshot, "recommendations", "snapshot"
    )
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


def ordered_draft_sections(sections):
    """Snapshot draft sections in explicit ``order``, then input order."""
    indexed = [
        (index, section)
        for index, section in enumerate(sections or [])
        if isinstance(section, dict)
    ]
    indexed.sort(
        key=lambda pair: (
            pair[1].get("order") is None,
            pair[1].get("order") if pair[1].get("order") is not None else 0,
            pair[0],
        )
    )
    return [section for _, section in indexed]


def pending_review_sections(sections):
    """Unapproved draft sections. Never treated as a final bid response."""
    return [
        section
        for section in ordered_draft_sections(sections)
        if not is_approved_section(section)
    ]


def draft_review_status_label(section):
    """Visible review label. Unapproved sections are never shown as APPROVED."""
    if is_approved_section(section):
        return APPROVED
    return DRAFT_PENDING_REVIEW_LABEL


def draft_requirement_ids_text(section):
    ids = []
    seen = set()
    for item in section.get("sourceRequirementIds") or []:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in seen:
            seen.add(text)
            ids.append(text)
    return ", ".join(ids)


def draft_source_references_text(section):
    citations = []
    seen = set()
    for source in section.get("sources") or []:
        if not isinstance(source, dict):
            continue
        filename = source.get("filename")
        filename = filename.strip() if isinstance(filename, str) else ""
        page = source.get("page")
        page_text = str(page).strip() if page is not None else ""
        clause = source.get("clause")
        clause = clause.strip() if isinstance(clause, str) else ""
        parts = []
        if filename:
            parts.append(filename)
        if page_text:
            parts.append(f"p.{page_text}")
        if clause:
            parts.append(f"clause {clause}")
        bit = ", ".join(parts)
        if bit and bit not in seen:
            seen.add(bit)
            citations.append(bit)
    return "; ".join(citations)


def draft_missing_information_text(section):
    gaps = []
    seen = set()
    for item in section.get("missingInformation") or []:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text and text not in seen:
            seen.add(text)
            gaps.append(text)
    return "; ".join(gaps)


def draft_reviewer_comments_text(section):
    """Copy reviewer comments from the snapshot only. Never invent them."""
    for key in ("reviewerComments", "comments", "comment"):
        value = section.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def draft_generated_content(section):
    content = section.get("content") if isinstance(section, dict) else None
    if isinstance(content, str) and content.strip():
        return content
    return MISSING_DRAFT_CONTENT


def expected_draft_specs():
    from app.drafting.generator import SECTION_SPECS
    return SECTION_SPECS


def _draft_identity_keys(section):
    keys = set()
    section_type = section.get("sectionType")
    if isinstance(section_type, str) and section_type.strip():
        keys.add(section_type.strip())
    title = section.get("title")
    if isinstance(title, str) and title.strip():
        keys.add(title.strip())
    return keys


def missing_expected_draft_specs(sections):
    """Expected bid-response types with no snapshot row (failed or omitted)."""
    specs = expected_draft_specs()
    known_types = {item[0] for item in specs}
    known_titles = {item[1] for item in specs}
    uses_expected_types = False
    present = set()
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        keys = _draft_identity_keys(section)
        present.update(keys)
        if keys & known_types or keys & known_titles:
            uses_expected_types = True
    if not uses_expected_types:
        return []
    missing = []
    for section_type, title, order in specs:
        if section_type in present or title in present:
            continue
        missing.append((section_type, title, order))
    return missing


SOURCE_VALIDATION_INCOMPLETE = "incomplete"
SOURCE_VALIDATION_COMPLETE = "complete"
SOURCE_VALIDATION_UNKNOWN = "unknown"
ACTUAL_PDF_PAGE = "actual_pdf_page"
DEMO_PENDING_SOURCE_VALIDATION = (
    "Demonstration — pending source validation. "
    "Some source page references are approximate document boundaries, "
    "not verified PDF page numbers. Do not treat page numbers as final."
)


def source_page_reference_types(snapshot):
    """Collect page_reference_type values already present on sources."""
    types = []
    for requirement in snapshot.get("requirements") or []:
        if not isinstance(requirement, dict):
            continue
        for source in requirement.get("sources") or []:
            if not isinstance(source, dict):
                continue
            value = source.get("pageReferenceType")
            if value is None:
                value = source.get("page_reference_type")
            if isinstance(value, str) and value.strip():
                types.append(value.strip())
    return types


def source_validation_status(snapshot):
    """complete / incomplete / unknown from existing source fields only."""
    types = source_page_reference_types(snapshot)
    if not types:
        return SOURCE_VALIDATION_UNKNOWN
    if all(value == ACTUAL_PDF_PAGE for value in types):
        return SOURCE_VALIDATION_COMPLETE
    return SOURCE_VALIDATION_INCOMPLETE


def source_validation_banner(snapshot):
    """Human-readable notice when source verification is incomplete, else None."""
    if source_validation_status(snapshot) == SOURCE_VALIDATION_INCOMPLETE:
        return DEMO_PENDING_SOURCE_VALIDATION
    return None
