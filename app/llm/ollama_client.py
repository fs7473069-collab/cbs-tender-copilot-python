
import copy
import json
import os
import re
import threading
import time

import requests


OLLAMA_URL = os.getenv(
    "OLLAMA_URL",
    "http://localhost:11434/api/generate",
)
MODEL_NAME = os.getenv("OLLAMA_MODEL", "qwen3:8b")
# Bump when the extract_requirements prompt or its output rules change.
EXTRACTION_PROMPT_VERSION = "rfp-extraction-v1"
TENDER_SUMMARY_PROMPT_VERSION = "tender-summary-v1"
TENDER_SUMMARY_FIELDS = (
    "title",
    "customerName",
    "tenderReference",
    "submissionDeadline",
    "summary",
)
# Caps runaway decoding on CPU; typical extraction responses are far smaller.
EXTRACT_NUM_PREDICT = int(os.getenv("OLLAMA_EXTRACT_NUM_PREDICT", "1536"))
# Optional CPU thread hint for Ollama. 0 leaves Ollama's default.
OLLAMA_NUM_THREAD = int(os.getenv("OLLAMA_NUM_THREAD", "0"))
# Documented production defaults (unchanged). Live values are read at call time
# via relevance_batch_* helpers so process env is honored after import.
RELEVANCE_BATCH_TIMEOUT_DEFAULT = 120
RELEVANCE_BATCH_MAX_PAIRS_DEFAULT = 0
# Backward-compatible aliases (import-time snapshot; prefer helpers in new code).
RELEVANCE_BATCH_TIMEOUT = int(
    os.getenv(
        "OLLAMA_RELEVANCE_BATCH_TIMEOUT",
        str(RELEVANCE_BATCH_TIMEOUT_DEFAULT),
    )
)
RELEVANCE_BATCH_MAX_PAIRS = int(
    os.getenv(
        "OLLAMA_RELEVANCE_BATCH_MAX_PAIRS",
        str(RELEVANCE_BATCH_MAX_PAIRS_DEFAULT),
    )
)
BATCH_TIMEOUT_UNASSESSED_REASON = (
    "Batch relevance timed out; left unassessed (not accepted as evidence)."
)


def relevance_batch_timeout_seconds():
    """Client timeout for batch relevance generate (seconds).

    Reads ``OLLAMA_RELEVANCE_BATCH_TIMEOUT`` at call time so benchmarks can set
    the env before invoking without re-importing this module. Default remains
    120 when unset.
    """
    raw = os.getenv("OLLAMA_RELEVANCE_BATCH_TIMEOUT")
    if raw is None or raw.strip() == "":
        return RELEVANCE_BATCH_TIMEOUT_DEFAULT
    return int(raw)


def relevance_batch_max_pairs():
    """Optional max pairs per batch generate; 0 means no split.

    Reads ``OLLAMA_RELEVANCE_BATCH_MAX_PAIRS`` at call time. Production default
    is 0 (single batch for the full round).
    """
    raw = os.getenv("OLLAMA_RELEVANCE_BATCH_MAX_PAIRS")
    if raw is None or raw.strip() == "":
        return RELEVANCE_BATCH_MAX_PAIRS_DEFAULT
    return int(raw)

# Obligation / priority signals on whitespace-normalized text (PDF newlines).
# Do not treat bare "S"/"M"/"L" as priority — site codes like "S-01" are common.
_REQUIREMENT_SIGNAL = re.compile(
    r"(?i)\b("
    r"shall|must|should|required|requirement|mandatory|"
    r"vendor|responsible|obligation"
    r")\b|"
    r"\bpriority\b\s*[:=]?\s*[MSL]\b"
)

# Reference layouts that are safe to skip only when no requirement signals exist.
_SKIP_GLOSSARY = re.compile(r"(?i)\bterm\b\s+\bdefinition\b")
_SKIP_SITE_INVENTORY = re.compile(
    r"(?i)("
    r"\bref\b.+\blocation\s+type\b.+\bcharger\s+type\b"
    r"|"
    r"\bconnectors\b.+\bS-\d{2}\b.+\b(?:rapid|non-?\s*rapid)\b"
    r")"
)
_SKIP_EVAL_WEIGHTING = re.compile(
    r"(?i)(\d{1,3}\s*%)(?:[^\d%]{0,100}\d{1,3}\s*%){3,}"
)
# Two or more "ACRONYM …: the/a/an …" definition entries without obligation words.
_SKIP_ACRONYM_GLOSSARY = re.compile(
    r"(?i)(?:\b[A-Z]{2,12}\b[^.:]{0,80}:\s+(?:the|a|an)\b.*){2,}",
)
_SKIP_BLANK_PAGE = re.compile(
    r"(?i)\b(?:this\s+page\s+)?intentionally\s+left\s+blank\b"
)
# Classic TOC leaders (title ...... 12). Requires several entries; never used
# when an obligation/priority signal is present.
_SKIP_TOC_DOTTED = re.compile(r"\.{3,}\s*\d+\b")
_TOC_DOTTED_MIN_HITS = 3

# Reuse extraction JSON for identical chunk text within one process (same tender
# often repeats banners). Bounded to avoid unbounded growth on long consumers.
_EXTRACT_RESULT_CACHE = {}
_EXTRACT_RESULT_CACHE_MAX = 256
# Chunk extraction may run in worker threads; guards cache reads and writes.
_EXTRACT_RESULT_CACHE_LOCK = threading.Lock()

# Last /api/generate metrics (done_reason, response_chars) for batch diagnostics.
# Thread-local so concurrent generates never overwrite each other's metrics.
_GENERATE_METRICS_LOCAL = threading.local()
# Leaf multi-pair batch diagnostic events (benchmark / offline inspection).
_BATCH_DIAGNOSTIC_EVENTS = []


def get_last_generate_metrics():
    """Return a copy of the calling thread's most recent generate metrics."""
    return dict(getattr(_GENERATE_METRICS_LOCAL, "metrics", {}))


def _clear_last_generate_metrics():
    _GENERATE_METRICS_LOCAL.metrics = {}


def clear_extract_requirements_cache():
    """Clear the in-process identical-chunk extraction cache (tests/helpers)."""
    with _EXTRACT_RESULT_CACHE_LOCK:
        _EXTRACT_RESULT_CACHE.clear()


def clear_batch_diagnostic_events():
    """Clear recorded batch diagnostic events (benchmarks/tests)."""
    _BATCH_DIAGNOSTIC_EVENTS.clear()


def get_batch_diagnostic_events():
    """Return a copy of recorded leaf-batch diagnostic events."""
    return list(_BATCH_DIAGNOSTIC_EVENTS)


def normalize_chunk_text(text):
    return " ".join((text or "").split())


def chunk_extraction_decision(text):
    """Decide whether extract_requirements should call the LLM.

    Returns ``(should_extract, reason)``. Any obligation/priority signal forces
    extraction. Reference layouts are skipped only when those signals are
    absent. Chunks without an obvious keyword still extract (default True) so
    ownership/priority tables are not dropped by keyword absence alone.
    """
    if not isinstance(text, str) or not text.strip():
        return False, "empty_text"

    normalized = normalize_chunk_text(text)
    if _REQUIREMENT_SIGNAL.search(normalized):
        return True, "requirement_signal"

    if _SKIP_GLOSSARY.search(normalized):
        return False, "glossary_term_definition"
    if _SKIP_ACRONYM_GLOSSARY.search(normalized):
        return False, "acronym_glossary"
    if _SKIP_SITE_INVENTORY.search(normalized):
        return False, "site_inventory"
    if _SKIP_EVAL_WEIGHTING.search(normalized):
        return False, "evaluation_weighting"
    if _SKIP_BLANK_PAGE.search(normalized):
        return False, "intentionally_left_blank"
    if len(_SKIP_TOC_DOTTED.findall(normalized)) >= _TOC_DOTTED_MIN_HITS:
        return False, "table_of_contents_dotted"

    return True, "default_extract"


def chunk_may_contain_requirements(text):
    """Return False only for clearly non-requirement reference material."""
    should_extract, _reason = chunk_extraction_decision(text)
    return should_extract


def _log_generate_metrics(
    label, wall_seconds, body, prompt_chars, num_predict, model=None
):
    """Emit one structured timing line for a generate call."""
    eval_count = body.get("eval_count")
    eval_duration = body.get("eval_duration")
    prompt_eval_count = body.get("prompt_eval_count")
    prompt_eval_duration = body.get("prompt_eval_duration")
    tokens_per_second = None
    if isinstance(eval_count, int) and isinstance(eval_duration, (int, float)) and eval_duration > 0:
        tokens_per_second = round(eval_count / (eval_duration / 1e9), 3)
    response_chars = len(body.get("response") or "")
    done_reason = body.get("done_reason")
    _GENERATE_METRICS_LOCAL.metrics = {
        "label": label,
        "done_reason": done_reason,
        "response_chars": response_chars,
        "wall_seconds": round(wall_seconds, 3),
        "eval_count": eval_count,
    }
    print(
        "OLLAMA_GENERATE "
        f"label={label} model={model or MODEL_NAME} wall_s={wall_seconds:.3f} "
        f"prompt_chars={prompt_chars} num_predict={num_predict} "
        f"prompt_eval_count={prompt_eval_count} "
        f"prompt_eval_s={None if not prompt_eval_duration else round(prompt_eval_duration / 1e9, 3)} "
        f"eval_count={eval_count} "
        f"eval_s={None if not eval_duration else round(eval_duration / 1e9, 3)} "
        f"gen_tok_s={tokens_per_second} "
        f"done_reason={done_reason} "
        f"response_chars={response_chars}",
        flush=True,
    )


def generate_response(
    prompt,
    *,
    num_predict=4096,
    timeout=300,
    label="generate",
    model=None,
):
    """Generate a non-streaming JSON response from Ollama."""
    options = {"num_predict": num_predict}
    if OLLAMA_NUM_THREAD > 0:
        options["num_thread"] = OLLAMA_NUM_THREAD
    model_name = model or MODEL_NAME

    started = time.perf_counter()
    response = requests.post(
        OLLAMA_URL,
        json={
            "model": model_name,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "format": "json",
            "options": options,
        },
        timeout=timeout,
    )
    wall_seconds = time.perf_counter() - started
    response.raise_for_status()

    body = response.json()
    _log_generate_metrics(
        label, wall_seconds, body, len(prompt or ""), num_predict, model=model_name
    )

    generated_text = body.get("response", "")

    if not isinstance(generated_text, str) or not generated_text.strip():
        raise ValueError("Ollama returned an empty response.")

    return generated_text


def extract_requirements(chunk):
    """Extract explicit requirements from tender text."""
    tender_text = chunk.get("text", "")

    if not isinstance(tender_text, str) or not tender_text.strip():
        print(
            "OLLAMA_GENERATE skipped extract_requirements "
            "reason=empty_text cache=0",
            flush=True,
        )
        return {"requirements": []}

    should_extract, skip_reason = chunk_extraction_decision(tender_text)
    if not should_extract:
        print(
            "OLLAMA_GENERATE skipped extract_requirements "
            f"reason={skip_reason} cache=0",
            flush=True,
        )
        return {"requirements": []}

    cache_key = normalize_chunk_text(tender_text)
    with _EXTRACT_RESULT_CACHE_LOCK:
        cached = _EXTRACT_RESULT_CACHE.get(cache_key)
        if cached is not None:
            cached = copy.deepcopy(cached)
    if cached is not None:
        print(
            "OLLAMA_GENERATE skipped extract_requirements "
            f"reason=duplicate_chunk_text cache=1 "
            f"chars={len(cache_key)}",
            flush=True,
        )
        return cached

    prompt = f"""
You are a careful tender requirement extraction system.

Extract genuine, explicit requirements from the tender text.

Return ONLY valid JSON. Do not explain or include markdown.

GENERAL RULES:
- Use only information explicitly supported by the source.
- Do not invent requirements, classifications, facts, or obligations.
- Extract complete, standalone requirements.
- Keep each requirement text to one clear sentence when possible.
- When a source table row lists multiple option columns, each extracted ownership requirement must preserve the assignment for every option present in that row; do not stop after summarizing only some columns.
- Do not extract document metadata, headings, glossary definitions,
  table headers, or general background information.
- Do not turn scope exclusions, assumptions, or descriptive statements
  into requirements unless they state an actual obligation or deliverable.
- Avoid duplicate requirements.
- Do not include page numbers in the JSON.
- Do not extract suggested demo scripts, examples, or illustrative
  workflows as separate requirements unless they state a distinct,
  testable obligation.
- Do not combine multiple user actions into one requirement.
- Do not extract incomplete sentences or fragments.
- Do not extract section summaries as requirements.
- Preserve the source meaning. Do not turn a recommendation into
  a mandatory obligation.
- If there are no requirements, return {{"requirements": []}}.

CATEGORIES:
Use exactly one of:
- Functional
- Technical
- Security
- Commercial
- Unclear

MANDATORY CLASSIFICATION:
- The mandatory field represents the tender's explicit classification.
- Set mandatory=true only when the source explicitly marks the
  requirement as M or explicitly identifies it as mandatory.
- Set mandatory=false when the source explicitly marks it as S or L,
  or when no explicit mandatory classification is provided.
- Do not infer an M classification from words such as "must", "shall",
  "required", or "should" alone.
- Do not invent an M, S, or L classification.
- When the classification is absent or unclear, use mandatory=false.
  This does not mean the requirement is unimportant or optional in practice.

CONFIDENCE:
- Use exactly one of: High, Medium, or Low.
- High: clearly stated and unambiguous.
- Medium: supported but needs some interpretation.
- Low: ambiguous or uncertain as a requirement.
- Omit a statement if it is too ambiguous to establish a genuine
  requirement.

JSON FORMAT:
{{
  "requirements": [
    {{
      "text": "The complete requirement text",
      "category": "Functional",
      "mandatory": true,
      "confidence": "High"
    }}
  ]
}}

TENDER TEXT:
{tender_text}
"""

    response_text = generate_response(
        prompt,
        num_predict=EXTRACT_NUM_PREDICT,
        label="extract_requirements",
    )

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Ollama returned invalid JSON for requirement extraction."
        ) from exc

    if not isinstance(result, dict):
        raise ValueError(
            "Requirement extraction must return a JSON object."
        )

    requirements = result.get("requirements")

    if not isinstance(requirements, list):
        raise ValueError("'requirements' must be a list.")

    validated_requirements = []

    allowed_categories = {
        "Functional",
        "Technical",
        "Security",
        "Commercial",
        "Unclear",
    }
    allowed_confidence = {"High", "Medium", "Low"}

    for item in requirements:
        if not isinstance(item, dict):
            continue

        text = item.get("text")
        category = item.get("category")
        mandatory = item.get("mandatory")
        confidence = item.get("confidence")

        if not isinstance(text, str) or not text.strip():
            continue

        text = text.strip()

        # Reject incomplete statements ending in punctuation
        # that suggests the statement continues.
        if text.endswith((":", ";", "–", "-", ",")):
            continue

        # Reject known demo-script narration.
        lower_text = text.lower()
        if (
            "the journey above covers" in lower_text
            or "a demo that follows the journey" in lower_text
            or "suggested demo script" in lower_text
        ):
            continue

        if category not in allowed_categories:
            category = "Unclear"

        # Do not coerce strings such as "false" into booleans.
        if not isinstance(mandatory, bool):
            mandatory = False
            confidence = "Low"

        if confidence not in allowed_confidence:
            confidence = "Low"

        validated_requirements.append(
            {
                "text": text,
                "category": category,
                "mandatory": mandatory,
                "confidence": confidence,
            }
        )

    result = {"requirements": validated_requirements}
    with _EXTRACT_RESULT_CACHE_LOCK:
        if (
            cache_key not in _EXTRACT_RESULT_CACHE
            and len(_EXTRACT_RESULT_CACHE) >= _EXTRACT_RESULT_CACHE_MAX
        ):
            # Drop an arbitrary old entry; exact LRU is unnecessary for this cache.
            _EXTRACT_RESULT_CACHE.pop(next(iter(_EXTRACT_RESULT_CACHE)))
        _EXTRACT_RESULT_CACHE[cache_key] = copy.deepcopy(result)
    return result


def extract_tender_summary(tender_text):
    """Return explicitly stated cover details; null for anything not stated.

    Callers must still verify every value against the source text.
    """
    if not isinstance(tender_text, str) or not tender_text.strip():
        return {field: None for field in TENDER_SUMMARY_FIELDS}

    prompt = f"""
You extract cover details from the opening pages of a tender document.

Return ONLY valid JSON. Do not explain or include markdown.

RULES:
- Use only the TENDER TEXT below. Never guess or use outside knowledge.
- Copy every value exactly as it is written in the text.
- title: the tender or project name as printed.
- customerName: the organisation issuing the tender, as printed.
- tenderReference: the tender, notice, or reference number as printed.
- submissionDeadline: the bid submission deadline date and time as printed.
- summary: one or two sentences copied from the text that state what is
  being procured or the scope of work.
- Use null for any field that is not explicitly stated in the text.

JSON FORMAT:
{{
  "title": null,
  "customerName": null,
  "tenderReference": null,
  "submissionDeadline": null,
  "summary": null
}}

TENDER TEXT:
{tender_text}
"""

    response_text = generate_response(
        prompt,
        num_predict=512,
        timeout=180,
        label="extract_tender_summary",
    )
    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Ollama returned invalid JSON for tender summary extraction."
        ) from exc
    if not isinstance(result, dict):
        raise ValueError("Tender summary extraction must return a JSON object.")
    return {
        field: (
            result[field].strip()
            if isinstance(result.get(field), str) and result[field].strip()
            else None
        )
        for field in TENDER_SUMMARY_FIELDS
    }


def assess_evidence_relevance(requirement, evidence_text):
    """
    Check topical relevance only.

    Relevance does not establish whether the requirement is compliant.
    """
    if not isinstance(requirement, str) or not requirement.strip():
        raise ValueError("Requirement text cannot be empty.")

    if not isinstance(evidence_text, str) or not evidence_text.strip():
        return {
            "relevant": False,
            "reason": "Evidence text is empty.",
        }

    prompt = f"""
You are a strict evidence relevance checker for a tender analysis system.

Decide whether the evidence passage substantively discusses the SAME
capability, feature, or obligation as the tender requirement.

RULES:
- A shared word alone is not enough.
- A bidder signing an attendance sheet is NOT evidence of an employee
  attendance management system.
- A general company profile is not proof of a specific capability.
- Do not infer capabilities that the passage does not describe.
- Do not treat topical relevance as proof of compliance.
- This is a relevance check, NOT a compliance decision.

Return only JSON:
{{
  "relevant": true,
  "reason": "Brief explanation"
}}

TENDER REQUIREMENT:
{requirement}

CBS EVIDENCE:
{evidence_text}
"""

    # Relevance responses are tiny; a bounded output limits needless decoding.
    response_text = generate_response(
        prompt,
        num_predict=256,
        timeout=90,
        label="assess_evidence_relevance",
    )

    try:
        result = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Ollama returned invalid JSON for evidence assessment."
        ) from exc

    return _parse_relevance_object(result)


def _parse_relevance_object(result):
    if not isinstance(result, dict):
        raise ValueError("Evidence assessment must be a JSON object.")
    relevant = result.get("relevant")
    if not isinstance(relevant, bool):
        raise ValueError(
            "Evidence assessment must contain a boolean 'relevant'."
        )
    reason = result.get("reason", "")
    if not isinstance(reason, str):
        reason = ""
    return {"relevant": relevant, "reason": reason}


def _coerce_relevant_bool(value):
    """Coerce common model JSON quirks to a bool; None if not a clear boolean."""
    if isinstance(value, bool):
        return value
    # bool is a subclass of int; the check above already handled True/False.
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().casefold()
        if lowered in {"true", "yes"}:
            return True
        if lowered in {"false", "no"}:
            return False
    return None


def _parse_relevance_object_batch(result):
    """Parse one batch assessment; tolerate string/0-1 ``relevant`` values.

    Single-pair ``assess_evidence_relevance`` stays strict. Batch responses from
    local Qwen frequently emit ``\"true\"``/``\"false\"`` strings; rejecting
    those discarded valid siblings and forced sequential fallbacks (measured:
    3-pair batches reusing only 1 assessment).
    """
    if not isinstance(result, dict):
        raise ValueError("Evidence assessment must be a JSON object.")
    relevant = _coerce_relevant_bool(result.get("relevant"))
    if relevant is None:
        raise ValueError(
            "Evidence assessment must contain a boolean 'relevant'."
        )
    reason = result.get("reason", "")
    if not isinstance(reason, str):
        reason = ""
    return {"relevant": relevant, "reason": reason}


def _loads_json_payload(response_text):
    """Parse a JSON object/array from a model response.

    Accepts a raw JSON document or the first balanced ``{...}`` / ``[...]``
    block when the model wraps JSON in prose. Does not invent values.
    """
    if not isinstance(response_text, str) or not response_text.strip():
        raise ValueError("Empty batch relevance response.")
    text = response_text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start < 0 or end <= start:
            continue
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            continue
    raise ValueError("Batch relevance response is not valid JSON.")


def _coerce_nonneg_int(raw):
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _pair_index_from_assessment(item, prepared_count, *, pair_index_one_based=False):
    """Return a 0-based prepared-pair index from optional id fields.

    ``pair_index`` is normally 0-based (batch prompt). When the whole response
    looks 1-based (values in 1..N, never 0), callers set
    ``pair_index_one_based=True``. ``id`` / ``pair`` stay 1-based.
    """
    if not isinstance(item, dict):
        return None
    if "pair_index" in item:
        value = _coerce_nonneg_int(item.get("pair_index"))
        if value is None:
            return None
        if pair_index_one_based:
            if 1 <= value <= prepared_count:
                return value - 1
            return None
        if 0 <= value < prepared_count:
            return value
        return None
    for key in ("id", "pair"):
        if key not in item:
            continue
        value = _coerce_nonneg_int(item.get(key))
        if value is not None and 1 <= value <= prepared_count:
            return value - 1
    return None


def _pair_index_values_look_one_based(assessments, prepared_count):
    """True when every item has pair_index in 1..N and none use 0."""
    if not isinstance(assessments, list) or not assessments:
        return False
    values = []
    for item in assessments:
        if not isinstance(item, dict) or "pair_index" not in item:
            return False
        value = _coerce_nonneg_int(item.get("pair_index"))
        if value is None:
            return False
        values.append(value)
    return (
        min(values) >= 1
        and max(values) <= prepared_count
        and 0 not in values
    )


def _assessments_list_from_payload(parsed):
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        assessments = parsed.get("assessments")
        if isinstance(assessments, list):
            return assessments
    return None


def _truncate_for_log(text, limit=800):
    if not isinstance(text, str):
        text = repr(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...<truncated chars={len(text)}>"


def _batch_assessment_structure_rows(assessments):
    """Summarize assessment entries for diagnostics (no tender/evidence text)."""
    if not isinstance(assessments, list):
        return {
            "assessment_count": None,
            "assessments_type": type(assessments).__name__,
            "items": [],
        }
    items = []
    for index, item in enumerate(assessments):
        if not isinstance(item, dict):
            items.append({
                "index": index,
                "type": type(item).__name__,
                "parseable": False,
                "error": "not_a_dict",
            })
            continue
        row = {
            "index": index,
            "keys": sorted(item.keys()),
            "pair_index": item.get("pair_index", "<missing>"),
            "id": item.get("id", "<missing>"),
            "relevant_type": type(item.get("relevant")).__name__,
            "relevant_repr": _truncate_for_log(repr(item.get("relevant")), 40),
            "reason_chars": (
                len(item.get("reason"))
                if isinstance(item.get("reason"), str)
                else None
            ),
        }
        try:
            _parse_relevance_object_batch(item)
            row["parseable"] = True
            row["error"] = None
        except ValueError as exc:
            row["parseable"] = False
            row["error"] = str(exc)
        items.append(row)
    return {
        "assessment_count": len(assessments),
        "assessments_type": "list",
        "items": items,
    }


def _parseable_assessment_count(assessments):
    """Count assessment rows that coerce through batch relevance parsing."""
    structure = _batch_assessment_structure_rows(assessments)
    return sum(1 for item in structure["items"] if item.get("parseable"))


def _done_reason_for_batch_label():
    """Return done_reason from the last generate if it was a batch call."""
    metrics = get_last_generate_metrics()
    if metrics.get("label") != "assess_evidence_relevance_batch":
        return None
    return metrics.get("done_reason")


def _record_batch_diagnostic_event(
    *,
    input_pairs,
    prepared,
    map_status,
    assessment_count,
    parseable_items,
    reused,
    missing,
    response_chars,
    done_reason,
    timed_out=False,
):
    """Append one leaf-batch diagnostic record (no tender/evidence text)."""
    event = {
        "input_pairs": input_pairs,
        "prepared": prepared,
        "map_status": map_status,
        "assessment_count": assessment_count,
        "parseable_items": parseable_items,
        "reused": reused,
        "missing": missing,
        "response_chars": response_chars,
        "done_reason": done_reason,
        "timed_out": timed_out,
    }
    _BATCH_DIAGNOSTIC_EVENTS.append(event)
    print(
        "OLLAMA_RELEVANCE_BATCH diagnostic "
        f"input_pairs={input_pairs} prepared={prepared} "
        f"map_status={map_status} "
        f"assessment_count={assessment_count} "
        f"parseable_items={parseable_items} "
        f"reused={reused} missing={missing} "
        f"response_chars={response_chars} "
        f"done_reason={done_reason} "
        f"timed_out={timed_out}",
        flush=True,
    )
    return event


def _log_batch_response_diagnostics(response_text, assessments, prepared_count):
    """Temporary diagnostics for live batch mapping failures.

    Logs truncated raw JSON plus structural fields only (not tender/evidence
    passages from the prompt).
    """
    structure = _batch_assessment_structure_rows(assessments)
    parseable = sum(
        1 for item in structure["items"] if item.get("parseable")
    )
    print(
        "OLLAMA_RELEVANCE_BATCH raw_response "
        f"chars={len(response_text or '')} "
        f"preview={_truncate_for_log(response_text or '', 800)!r}",
        flush=True,
    )
    print(
        "OLLAMA_RELEVANCE_BATCH structure "
        f"prepared={prepared_count} "
        f"assessment_count={structure['assessment_count']} "
        f"assessments_type={structure['assessments_type']} "
        f"parseable_items={parseable} "
        f"items={json.dumps(structure['items'], ensure_ascii=False)}",
        flush=True,
    )


def _map_batch_assessments(assessments, prepared_count):
    """Map model assessments onto prepared pair slots.

    Prefer explicit ``pair_index`` / ``id`` / ``pair`` when present so a
    missing or malformed item does not discard valid siblings. Fall back to
    positional mapping when lengths match and indexes are missing, colliding,
    or incomplete. Batch parsing tolerates string/0-1 ``relevant`` values.

    Returns ``(slots, status)`` where each slot is a parsed assessment or
    ``None``, and ``status`` is ``ok``, ``partial``, or an error token.
    """
    slots = [None] * prepared_count
    if not isinstance(assessments, list):
        return slots, "assessments_not_list"

    one_based = _pair_index_values_look_one_based(assessments, prepared_count)
    indexed_items = []
    unindexed_items = []
    for item in assessments:
        pair_index = _pair_index_from_assessment(
            item, prepared_count, pair_index_one_based=one_based
        )
        if pair_index is None:
            unindexed_items.append(item)
        else:
            indexed_items.append((pair_index, item))

    if indexed_items:
        for pair_index, item in indexed_items:
            try:
                parsed = _parse_relevance_object_batch(item)
            except ValueError:
                # Do not clear a previously valid slot for this pair_index.
                continue
            # First valid assessment for a pair wins; later duplicates ignored.
            if slots[pair_index] is None:
                slots[pair_index] = parsed
        # Positional fill only for remaining empty slots, in order.
        empties = [index for index, value in enumerate(slots) if value is None]
        for item, slot_index in zip(unindexed_items, empties):
            try:
                slots[slot_index] = _parse_relevance_object_batch(item)
            except ValueError:
                continue
    else:
        if len(assessments) != prepared_count:
            return slots, "count_mismatch_without_ids"
        for index, item in enumerate(assessments):
            try:
                slots[index] = _parse_relevance_object_batch(item)
            except ValueError:
                # Keep slot empty for selective sequential fallback.
                continue

    filled = sum(1 for value in slots if value is not None)
    # Rescue: model reused the same pair_index (e.g. all 0) or left gaps, but
    # returned one assessment per pair in order — map by position.
    if (
        filled < prepared_count
        and len(assessments) == prepared_count
    ):
        for index, item in enumerate(assessments):
            if slots[index] is not None:
                continue
            try:
                slots[index] = _parse_relevance_object_batch(item)
            except ValueError:
                continue
        filled = sum(1 for value in slots if value is not None)

    if filled == prepared_count:
        return slots, "ok"
    if filled == 0:
        return slots, "no_valid_assessments"
    return slots, "partial"


def _unassessed_timeout_result():
    """Safe placeholder when a batch generate times out.

    ``relevant=False`` so callers never accept the passage; ``unassessed=True``
    so the matcher must not persist this as a real relevance decision.
    """
    return {
        "relevant": False,
        "reason": BATCH_TIMEOUT_UNASSESSED_REASON,
        "unassessed": True,
    }


def assess_evidence_relevance_batch(pairs):
    """Assess several requirement/evidence pairs in one generate call.

    Parse failures reuse valid batch items and sequentially assess only the
    missing pairs. Client timeouts do **not** trigger sequential retries: the
    local model may still be busy serving the abandoned batch request, and
    N follow-up generates amplify load. Timed-out pairs are left unassessed
    (not accepted as evidence). Does not invent relevance.
    """
    if not isinstance(pairs, list):
        raise ValueError("pairs must be a list.")
    if not pairs:
        return []

    # Optional benchmark/ops split; production default 0 keeps one batch call.
    max_pairs = relevance_batch_max_pairs()
    if max_pairs > 0 and len(pairs) > max_pairs:
        print(
            "OLLAMA_RELEVANCE_BATCH split "
            f"input_pairs={len(pairs)} "
            f"max_pairs={max_pairs}",
            flush=True,
        )
        combined = []
        for start in range(0, len(pairs), max_pairs):
            combined.extend(
                assess_evidence_relevance_batch(
                    pairs[start : start + max_pairs]
                )
            )
        return combined

    if len(pairs) == 1:
        requirement, evidence_text = pairs[0]
        print(
            "OLLAMA_RELEVANCE_BATCH single_pair path=assess_evidence_relevance "
            "pairs=1",
            flush=True,
        )
        return [assess_evidence_relevance(requirement, evidence_text)]

    prepared = []
    empty_indexes = set()
    for index, pair in enumerate(pairs):
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError("Each pair must be (requirement, evidence_text).")
        requirement, evidence_text = pair
        if not isinstance(requirement, str) or not requirement.strip():
            raise ValueError("Requirement text cannot be empty.")
        if not isinstance(evidence_text, str) or not evidence_text.strip():
            empty_indexes.add(index)
            continue
        prepared.append((index, requirement.strip(), evidence_text.strip()))

    if not prepared:
        return [
            {"relevant": False, "reason": "Evidence text is empty."}
            for _ in pairs
        ]

    batch_timeout = relevance_batch_timeout_seconds()
    print(
        "OLLAMA_RELEVANCE_BATCH invoke "
        f"input_pairs={len(pairs)} prepared={len(prepared)} "
        f"empty={len(empty_indexes)} "
        f"timeout_s={batch_timeout}",
        flush=True,
    )

    numbered = [
        (
            f"PAIR {position}:\n"
            f"TENDER REQUIREMENT:\n{requirement}\n"
            f"CBS EVIDENCE:\n{evidence_text}"
        )
        for position, (_index, requirement, evidence_text) in enumerate(prepared)
    ]

    prompt = f"""
You are a strict evidence relevance checker for a tender analysis system.

For EACH numbered pair, decide whether the evidence passage substantively
discusses the SAME capability, feature, or obligation as the tender
requirement.

RULES:
- A shared word alone is not enough.
- A bidder signing an attendance sheet is NOT evidence of an employee
  attendance management system.
- A general company profile is not proof of a specific capability.
- Do not infer capabilities that the passage does not describe.
- Do not treat topical relevance as proof of compliance.
- Return exactly one assessment object for every numbered PAIR below.
- assessments length must equal the number of PAIR blocks.
- Each assessment must include pair_index matching that PAIR number
  (0-based), relevant as a JSON boolean (true or false), and reason.
- Do not omit pairs, merge pairs, or return fewer assessments than pairs.
- This is a relevance check, NOT a compliance decision.

Return only valid JSON. Example shape for two pairs:
{{
  "assessments": [
    {{"pair_index": 0, "relevant": true, "reason": "Brief explanation"}},
    {{"pair_index": 1, "relevant": false, "reason": "Brief explanation"}}
  ]
}}

PAIRS:
{chr(10).join(numbered)}
"""

    # Bound decode by pair count; keep headroom for short reasons.
    num_predict = min(256 * len(numbered), 2048)
    prepared_slots = [None] * len(prepared)
    fallback_reason = None
    timed_out = False

    try:
        # Avoid attributing a prior generate's done_reason to this batch leaf.
        _clear_last_generate_metrics()
        response_text = generate_response(
            prompt,
            num_predict=num_predict,
            timeout=batch_timeout,
            label="assess_evidence_relevance_batch",
        )
        parsed = _loads_json_payload(response_text)
        assessments = _assessments_list_from_payload(parsed)
        # Temporary: capture raw/structure whenever mapping is incomplete so
        # live smoke runs can show assessment_count vs prepared (see logs).
        prepared_slots, map_status = _map_batch_assessments(
            assessments, len(prepared)
        )
        filled = sum(1 for item in prepared_slots if item is not None)
        assessment_count = (
            len(assessments) if isinstance(assessments, list) else None
        )
        parseable_items = _parseable_assessment_count(assessments)
        response_chars = len(response_text or "")
        done_reason = _done_reason_for_batch_label()
        _record_batch_diagnostic_event(
            input_pairs=len(pairs),
            prepared=len(prepared),
            map_status=map_status,
            assessment_count=assessment_count,
            parseable_items=parseable_items,
            reused=filled,
            missing=len(prepared) - filled,
            response_chars=response_chars,
            done_reason=done_reason,
            timed_out=False,
        )
        if map_status == "ok":
            print(
                "OLLAMA_RELEVANCE_BATCH success "
                f"prepared={len(prepared)} parsed={filled} "
                f"assessment_count={assessment_count} "
                f"parseable_items={parseable_items} "
                f"response_chars={response_chars} "
                f"done_reason={done_reason} "
                "fallback_pairs=0",
                flush=True,
            )
        else:
            fallback_reason = map_status
            _log_batch_response_diagnostics(
                response_text, assessments, len(prepared)
            )
            print(
                "OLLAMA_RELEVANCE_BATCH map "
                f"status={map_status} prepared={len(prepared)} "
                f"assessment_count={assessment_count} "
                f"parseable_items={parseable_items} "
                f"reused={filled} missing={len(prepared) - filled} "
                f"response_chars={response_chars} "
                f"done_reason={done_reason}",
                flush=True,
            )
    except requests.Timeout as exc:
        # Client abandoned the wait; Ollama may still be generating. Do not
        # enqueue N sequential relevance calls on top of that work.
        timed_out = True
        fallback_reason = f"Timeout:{exc}"
        prepared_slots = [_unassessed_timeout_result() for _ in prepared]
        _record_batch_diagnostic_event(
            input_pairs=len(pairs),
            prepared=len(prepared),
            map_status="timeout",
            assessment_count=None,
            parseable_items=None,
            reused=0,
            missing=len(prepared),
            response_chars=None,
            done_reason=None,
            timed_out=True,
        )
        print(
            "OLLAMA_RELEVANCE_BATCH timeout "
            f"reason={fallback_reason} input_pairs={len(pairs)} "
            f"prepared={len(prepared)} "
            f"skipped_unassessed={len(prepared)} sequential_pairs=0 "
            f"policy=leave_unassessed timeout_s={batch_timeout}",
            flush=True,
        )
    except (ValueError, json.JSONDecodeError, OSError, requests.RequestException) as exc:
        fallback_reason = f"{type(exc).__name__}:{exc}"
        prepared_slots = [None] * len(prepared)
        _record_batch_diagnostic_event(
            input_pairs=len(pairs),
            prepared=len(prepared),
            map_status=f"error:{type(exc).__name__}",
            assessment_count=None,
            parseable_items=None,
            reused=0,
            missing=len(prepared),
            response_chars=None,
            done_reason=None,
            timed_out=False,
        )

    if timed_out:
        results = [
            {"relevant": False, "reason": "Evidence text is empty."}
            if index in empty_indexes
            else None
            for index in range(len(pairs))
        ]
        for (index, _requirement, _evidence), assessment in zip(
            prepared, prepared_slots
        ):
            results[index] = assessment
        return results

    missing_prepared = [
        prepared_offset
        for prepared_offset, assessment in enumerate(prepared_slots)
        if assessment is None
    ]
    if missing_prepared:
        reason = fallback_reason or "incomplete_batch_parse"
        print(
            "OLLAMA_RELEVANCE_BATCH fallback "
            f"reason={reason} input_pairs={len(pairs)} "
            f"prepared={len(prepared)} "
            f"reused={len(prepared) - len(missing_prepared)} "
            f"sequential_pairs={len(missing_prepared)}",
            flush=True,
        )
        for prepared_offset in missing_prepared:
            _pair_index, requirement, evidence_text = prepared[prepared_offset]
            prepared_slots[prepared_offset] = assess_evidence_relevance(
                requirement, evidence_text
            )
    elif fallback_reason is None:
        # Full success already logged above.
        pass

    results = [
        {"relevant": False, "reason": "Evidence text is empty."}
        if index in empty_indexes
        else None
        for index in range(len(pairs))
    ]
    for (index, _requirement, _evidence), assessment in zip(
        prepared, prepared_slots
    ):
        results[index] = assessment
    return results
