"""Bid-response drafting with Qwen3:8B. Snapshot-side; not a compliance decision."""

import json
import os
import time
from datetime import datetime, timezone

import requests

from app.export.snapshot import APPROVED, is_approved_section
from app.llm.ollama_client import MODEL_NAME, generate_response


DRAFT_STATUS = "DRAFT"
PROTECTED_APPROVAL = frozenset({
    APPROVED,
    "REJECTED",
    "PENDING",
})
SECTION_SPECS = (
    ("CAPABILITY_RESPONSE", "Executive Summary", 1),
    ("UNDERSTANDING_OF_REQUIREMENTS", "Understanding of Requirements", 2),
    ("TECHNICAL_APPROACH", "Proposed Technical Approach", 3),
    ("IMPLEMENTATION_METHODOLOGY", "Implementation Methodology", 4),
    ("RELEVANT_EXPERIENCE", "Company Experience and Capabilities", 5),
    ("COMPLIANCE_RESPONSE", "Compliance Response", 6),
)
EXPERIENCE_TYPES = frozenset({"CAPABILITY_RESPONSE", "RELEVANT_EXPERIENCE"})
ALWAYS_MANDATORY_SECTIONS = frozenset({
    "UNDERSTANDING_OF_REQUIREMENTS",
    "COMPLIANCE_RESPONSE",
})
SECTION_KEYWORDS = {
    "CAPABILITY_RESPONSE": (
        "capability", "experience", "deliver", "provide", "solution",
        "service", "sso", "portal", "support", "implement", "login",
    ),
    "UNDERSTANDING_OF_REQUIREMENTS": (),
    "TECHNICAL_APPROACH": (
        "technical", "architect", "system", "integrat", "sso", "api",
        "security", "software", "platform", "infrastructure", "login",
        "authent", "network", "database", "interface", "application",
        "rest",
    ),
    "IMPLEMENTATION_METHODOLOGY": (
        "implement", "methodolog", "timeline", "phase", "rollout",
        "train", "migrat", "project", "plan", "deliverable", "milestone",
        "support", "operat",
    ),
    "RELEVANT_EXPERIENCE": (
        "experience", "capability", "similar", "past", "certif",
        "staff", "resource", "previous", "client", "portfolio",
    ),
    "COMPLIANCE_RESPONSE": (
        "complian", "mandatory", "shall", "must", "standard", "iso",
        "sla", "support", "legal", "regulat", "audit", "security",
    ),
}
MAX_REQUIREMENTS = 16
MIN_REQUIREMENTS = 4
MAX_REQUIREMENT_CHARS = 400
MAX_EVIDENCE_CHARS = 220
MAX_EVIDENCE_PER_REQUIREMENT = 2
try:
    DRAFT_NUM_PREDICT = int(os.getenv("OLLAMA_DRAFT_NUM_PREDICT", "1024"))
except (TypeError, ValueError):
    DRAFT_NUM_PREDICT = 1024
if DRAFT_NUM_PREDICT <= 0:
    DRAFT_NUM_PREDICT = 1024
DRAFT_TIMEOUT_SECONDS = 120
NO_EVIDENCE_GAP = (
    "No verified CBS knowledge-base evidence was available for past-performance "
    "or capability claims in this section."
)


def _utc_now():
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _requirement_id(requirement):
    for key in ("sourceItemKey", "requirementId"):
        value = requirement.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _trim(value, limit):
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    trimmed = text[:limit].rsplit(" ", 1)[0]
    return (trimmed or text[:limit]) + "…"


def _evidence_items(requirement):
    match = requirement.get("evidenceMatch")
    if not isinstance(match, dict):
        return []
    items = match.get("evidence")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _has_kb_evidence(requirements):
    return any(_evidence_items(item) for item in requirements)


def _requirement_text(requirement):
    parts = []
    for key in ("content", "text"):
        value = requirement.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return " ".join(parts).lower()


def _keyword_hits(section_type, requirement):
    keywords = SECTION_KEYWORDS.get(section_type) or ()
    text = _requirement_text(requirement)
    if not text:
        return 0
    return sum(1 for keyword in keywords if keyword in text)


def _is_relevant_mandatory(section_type, requirement):
    if requirement.get("mandatory") is not True:
        return False
    if section_type in ALWAYS_MANDATORY_SECTIONS:
        return True
    if _keyword_hits(section_type, requirement) > 0:
        return True
    if section_type in EXPERIENCE_TYPES and _evidence_items(requirement):
        return True
    return False


def _selection_score(section_type, requirement, index):
    hits = _keyword_hits(section_type, requirement)
    evidence = 8 if _evidence_items(requirement) else 0
    mandatory = 4 if requirement.get("mandatory") is True else 0
    return (hits, evidence, mandatory, -index)


def select_requirements_for_section(section_type, requirements):
    """Pick a per-section subset. Prefer relevant mandatories, capped for latency."""
    usable = [item for item in requirements if isinstance(item, dict)]
    if not usable:
        return []
    ranked_mandatory = sorted(
        (
            (index, item)
            for index, item in enumerate(usable)
            if _is_relevant_mandatory(section_type, item)
        ),
        key=lambda pair: _selection_score(section_type, pair[1], pair[0]),
        reverse=True,
    )
    selected = [item for _index, item in ranked_mandatory[:MAX_REQUIREMENTS]]
    selected_ids = {id(item) for item in selected}

    def is_topical(item):
        return _keyword_hits(section_type, item) > 0 or bool(_evidence_items(item))

    if len(selected) < MAX_REQUIREMENTS:
        ranked = sorted(
            (
                (index, item)
                for index, item in enumerate(usable)
                if id(item) not in selected_ids
            ),
            key=lambda pair: _selection_score(section_type, pair[1], pair[0]),
            reverse=True,
        )
        for _index, item in ranked:
            if len(selected) >= MAX_REQUIREMENTS:
                break
            if is_topical(item):
                selected.append(item)
    if not selected:
        selected = usable[:MIN_REQUIREMENTS]
    chosen = {id(item) for item in selected}
    return [item for item in usable if id(item) in chosen]


def _compact_requirements(requirements):
    compact = []
    for item in requirements:
        if not isinstance(item, dict):
            continue
        req_id = _requirement_id(item)
        if req_id is None:
            continue
        match = item.get("evidenceMatch") if isinstance(item.get("evidenceMatch"), dict) else {}
        evidence = []
        for passage in _evidence_items(item)[:MAX_EVIDENCE_PER_REQUIREMENT]:
            evidence.append({
                "source": passage.get("source"),
                "page": passage.get("page"),
                "excerpt": _trim(passage.get("text"), MAX_EVIDENCE_CHARS),
            })
        compact.append({
            "id": req_id,
            "mandatory": item.get("mandatory"),
            "content": _trim(item.get("content") or item.get("text"), MAX_REQUIREMENT_CHARS),
            "evidenceStatus": match.get("status"),
            "evidence": evidence,
        })
    return compact


def _source_entry(mapping):
    entry = {}
    for key in ("filename", "page", "clause", "section", "excerpt"):
        value = mapping.get(key)
        if value is None or value == "":
            continue
        if key == "excerpt" and isinstance(value, str):
            value = _trim(value, MAX_EVIDENCE_CHARS)
        entry[key] = value
    return entry


def _filenames_by_document_id(documents):
    mapping = {}
    for document in documents or []:
        if not isinstance(document, dict):
            continue
        document_id = document.get("documentId")
        filename = document.get("filename")
        if isinstance(document_id, str) and document_id.strip() and isinstance(filename, str) and filename.strip():
            mapping[document_id.strip()] = filename.strip()
    return mapping


def _sources_for_ids(requirements, requirement_ids, filenames_by_id=None):
    wanted = {item for item in requirement_ids if isinstance(item, str) and item.strip()}
    filenames_by_id = filenames_by_id or {}
    sources = []
    seen = set()
    for requirement in requirements:
        req_id = _requirement_id(requirement)
        if req_id not in wanted:
            continue
        for source in requirement.get("sources") or []:
            if not isinstance(source, dict):
                continue
            document_id = source.get("documentId")
            filename = source.get("filename")
            if not filename and isinstance(document_id, str):
                filename = filenames_by_id.get(document_id.strip())
            entry = _source_entry({
                "filename": filename,
                "page": source.get("pageNo", source.get("page")),
                "clause": source.get("clause"),
                "section": source.get("section"),
                "excerpt": source.get("excerpt"),
            })
            key = tuple(sorted(entry.items()))
            if entry and key not in seen:
                seen.add(key)
                sources.append(entry)
        for passage in _evidence_items(requirement):
            entry = _source_entry({
                "filename": passage.get("source"),
                "page": passage.get("page"),
                "excerpt": passage.get("text"),
            })
            key = tuple(sorted(entry.items()))
            if entry and key not in seen:
                seen.add(key)
                sources.append(entry)
    return sources


def _known_ids(requirements):
    ids = []
    for item in requirements:
        req_id = _requirement_id(item)
        if req_id:
            ids.append(req_id)
    return ids


def _filter_ids(raw_ids, known):
    allowed = set(known)
    selected = []
    for value in raw_ids or []:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if text in allowed and text not in selected:
            selected.append(text)
    if selected:
        return selected
    return list(known[:12])


def _section_prompt(section_type, title, tender, compact):
    tender = tender if isinstance(tender, dict) else {}
    return f"""
You draft one section of a CBS tender bid response.

Write professional bid/proposal language for THIS tender only.
Do not write a generic company brochure.

SECTION TYPE: {section_type}
SECTION TITLE: {title}

TENDER TITLE: {tender.get("title") or "Not provided"}
CUSTOMER: {tender.get("customerName") or "Not provided"}
REFERENCE: {tender.get("tenderReference") or "Not provided"}

REQUIREMENTS AND FILTERED CBS EVIDENCE (JSON):
{json.dumps(compact, ensure_ascii=False)}

RULES:
- Use only the requirements and evidence above.
- Cite requirement IDs from that list when a paragraph addresses them.
- If CBS evidence is present, treat it as candidate past work to be verified, not as proof of compliance.
- Distinguish proven CBS evidence from proposed future implementation.
- Never invent CBS clients, projects, certifications, staff, statistics, capabilities, or past experience.
- Never invent source filenames, pages, or clauses.
- Do not repeat the same paragraph.
- Mark gaps explicitly in missingInformation and in the prose with "Information Required:".
- Return JSON only:
{{
  "content": "Markdown-free prose with blank lines between paragraphs",
  "sourceRequirementIds": ["REQ-001"],
  "missingInformation": ["concise gap"]
}}
"""


def _strip_json_fences(text):
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.split("\n")
    body = lines[1:]
    if body and body[-1].strip().startswith("```"):
        body = body[:-1]
    return "\n".join(body).strip()


def _parse_draft_payload(raw):
    """Parse a complete draft JSON object. Truncated output is a failure."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("empty_draft_json")
    text = _strip_json_fences(raw)
    try:
        parsed, index = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError as exc:
        raise ValueError("truncated_or_malformed_json") from exc
    remainder = text[index:].strip()
    if remainder:
        raise ValueError("truncated_or_malformed_json")
    if not isinstance(parsed, dict):
        raise ValueError("invalid_draft_payload")
    content = parsed.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty_content")
    ids = parsed.get("sourceRequirementIds")
    if ids is None:
        raise ValueError("incomplete_draft_json")
    if not isinstance(ids, list):
        raise ValueError("invalid_draft_payload")
    missing = parsed.get("missingInformation", [])
    if missing is None:
        missing = []
    if not isinstance(missing, list):
        raise ValueError("invalid_draft_payload")
    return {
        "content": content.strip(),
        "sourceRequirementIds": ids,
        "missingInformation": [
            item.strip()
            for item in missing
            if isinstance(item, str) and item.strip()
        ],
    }


def _call_model(section_type, title, tender, compact):
    prompt = _section_prompt(section_type, title, tender, compact)
    print(
        "DRAFT_SECTION started "
        f"type={section_type} model={MODEL_NAME} "
        f"requirements={len(compact)}",
        flush=True,
    )
    started = time.perf_counter()
    raw = ""
    success = False
    error_type = None
    try:
        raw = generate_response(
            prompt,
            num_predict=DRAFT_NUM_PREDICT,
            timeout=DRAFT_TIMEOUT_SECONDS,
            label=f"draft_{section_type}",
        )
        payload = _parse_draft_payload(raw)
        success = True
        return payload
    except (requests.RequestException, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        error_type = type(exc).__name__
        if isinstance(exc, ValueError) and str(exc):
            error_type = str(exc) if " " not in str(exc) else type(exc).__name__
        return None
    finally:
        duration_ms = int((time.perf_counter() - started) * 1000)
        response_chars = len(raw) if isinstance(raw, str) else 0
        extra = f" error_type={error_type}" if error_type else ""
        print(
            "DRAFT_SECTION finished "
            f"type={section_type} success={str(success).lower()} "
            f"duration_ms={duration_ms} response_chars={response_chars}"
            f"{extra}",
            flush=True,
        )


def _missing_evidence_notes(requirements):
    notes = []
    for item in requirements:
        if _evidence_items(item):
            continue
        req_id = _requirement_id(item)
        if not req_id:
            continue
        if item.get("mandatory") is True:
            notes.append(
                f"No verified CBS evidence for mandatory requirement {req_id}."
            )
        else:
            notes.append(f"No verified CBS evidence for {req_id}.")
    return notes


def _honest_experience_gaps(section_type, content, missing, evidence_notes, has_evidence):
    gaps = list(missing)
    for note in evidence_notes:
        if note not in gaps:
            gaps.append(note)
    needs_experience_warning = (
        section_type in EXPERIENCE_TYPES
        and (evidence_notes or not has_evidence)
    )
    if not needs_experience_warning:
        return content, gaps
    if not has_evidence and NO_EVIDENCE_GAP not in gaps:
        gaps.append(NO_EVIDENCE_GAP)
    note = (
        "Information Required: Unverified or missing CBS past-performance "
        "evidence must not be treated as confirmed experience, clients, "
        "certifications, staffing, or statistics."
    )
    if "Information Required:" not in content:
        content = f"{content}\n\n{note}"
    return content, gaps


def _build_section(spec, payload, selected, all_requirements, filenames_by_id):
    section_type, title, order = spec
    known = _known_ids(selected)
    source_ids = _filter_ids(payload.get("sourceRequirementIds"), known)
    notes_source = (
        all_requirements if section_type in EXPERIENCE_TYPES else selected
    )
    content, missing = _honest_experience_gaps(
        section_type,
        payload["content"],
        payload.get("missingInformation") or [],
        _missing_evidence_notes(notes_source),
        _has_kb_evidence(notes_source),
    )
    return {
        "sectionId": f"DRAFT-{section_type}",
        "sectionType": section_type,
        "title": title,
        "content": content,
        "status": DRAFT_STATUS,
        "approvalStatus": DRAFT_STATUS,
        "sourceRequirementIds": source_ids,
        "sources": _sources_for_ids(all_requirements, source_ids, filenames_by_id),
        "missingInformation": missing,
        "generatedAt": _utc_now(),
        "model": MODEL_NAME,
        "order": order,
    }


def generate_draft_sections(tender, requirements, documents=None):
    """Generate up to six DRAFT sections. One failed section does not abort others."""
    if not isinstance(requirements, list) or not requirements:
        return []
    usable = [item for item in requirements if isinstance(item, dict)]
    if not usable:
        return []
    filenames_by_id = _filenames_by_document_id(documents)
    sections = []
    for spec in SECTION_SPECS:
        selected = select_requirements_for_section(spec[0], usable)
        compact = _compact_requirements(selected)
        if not compact:
            continue
        payload = _call_model(spec[0], spec[1], tender, compact)
        if payload is None:
            continue
        sections.append(
            _build_section(spec, payload, selected, usable, filenames_by_id)
        )
    print(
        "DRAFT_SECTIONS generated "
        f"count={len(sections)} model={MODEL_NAME}",
        flush=True,
    )
    return sections


def _approval_token(section):
    for key in ("approvalStatus", "status"):
        value = section.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _is_protected(section):
    if not isinstance(section, dict):
        return False
    if is_approved_section(section):
        return True
    token = _approval_token(section)
    if token in PROTECTED_APPROVAL:
        return True
    for key in ("approvedBy", "decidedBy", "approvedAt", "decidedAt"):
        value = section.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def merge_preserved_drafts(generated, existing):
    """Keep human/Java approved or in-review sections; fill remaining types."""
    preserved = []
    blocked_types = set()
    blocked_ids = set()
    for item in existing or []:
        if not _is_protected(item):
            continue
        preserved.append(dict(item))
        section_type = item.get("sectionType")
        section_id = item.get("sectionId")
        if isinstance(section_type, str) and section_type.strip():
            blocked_types.add(section_type.strip())
        if isinstance(section_id, str) and section_id.strip():
            blocked_ids.add(section_id.strip())
        title = item.get("title")
        if isinstance(title, str) and title.strip():
            blocked_types.add(title.strip())
    merged = list(preserved)
    for item in generated or []:
        if not isinstance(item, dict):
            continue
        section_type = item.get("sectionType")
        section_id = item.get("sectionId")
        title = item.get("title")
        if section_type in blocked_types or section_id in blocked_ids:
            continue
        if isinstance(title, str) and title.strip() in blocked_types:
            continue
        merged.append(dict(item))
    return merged
