
import json
import os
import re
import time

import requests


OLLAMA_URL = os.getenv(
    "OLLAMA_URL",
    "http://localhost:11434/api/generate",
)
MODEL_NAME = os.getenv("OLLAMA_MODEL", "qwen3:8b")
# Caps runaway decoding on CPU; typical extraction responses are far smaller.
EXTRACT_NUM_PREDICT = int(os.getenv("OLLAMA_EXTRACT_NUM_PREDICT", "1536"))
# Optional CPU thread hint for Ollama. 0 leaves Ollama's default.
OLLAMA_NUM_THREAD = int(os.getenv("OLLAMA_NUM_THREAD", "0"))

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


def normalize_chunk_text(text):
    return " ".join((text or "").split())


def chunk_may_contain_requirements(text):
    """Return False only for clearly non-requirement reference material.

    Any obligation/priority signal forces extraction. Reference layouts
    (glossary, site inventory, evaluation weighting) are skipped only when
    those signals are absent, so deliverable/ownership rows are never dropped.
    """
    if not isinstance(text, str) or not text.strip():
        return False

    normalized = normalize_chunk_text(text)
    if _REQUIREMENT_SIGNAL.search(normalized):
        return True

    if _SKIP_GLOSSARY.search(normalized):
        return False
    if _SKIP_ACRONYM_GLOSSARY.search(normalized):
        return False
    if _SKIP_SITE_INVENTORY.search(normalized):
        return False
    if _SKIP_EVAL_WEIGHTING.search(normalized):
        return False
    return True


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
    print(
        "OLLAMA_GENERATE "
        f"label={label} model={model or MODEL_NAME} wall_s={wall_seconds:.3f} "
        f"prompt_chars={prompt_chars} num_predict={num_predict} "
        f"prompt_eval_count={prompt_eval_count} "
        f"prompt_eval_s={None if not prompt_eval_duration else round(prompt_eval_duration / 1e9, 3)} "
        f"eval_count={eval_count} "
        f"eval_s={None if not eval_duration else round(eval_duration / 1e9, 3)} "
        f"gen_tok_s={tokens_per_second} "
        f"done_reason={body.get('done_reason')} "
        f"response_chars={len(body.get('response') or '')}",
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
        return {"requirements": []}

    if not chunk_may_contain_requirements(tender_text):
        print(
            "OLLAMA_GENERATE skipped extract_requirements "
            "(non-requirement reference chunk)",
            flush=True,
        )
        return {"requirements": []}

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

    return {"requirements": validated_requirements}


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


def assess_evidence_relevance_batch(pairs):
    """Assess several requirement/evidence pairs in one generate call.

    Falls back to sequential single-pair checks if the batch response is
    invalid. Does not invent relevance: failures degrade to per-pair calls
    or False when a pair cannot be assessed.
    """
    if not isinstance(pairs, list):
        raise ValueError("pairs must be a list.")
    if not pairs:
        return []
    if len(pairs) == 1:
        requirement, evidence_text = pairs[0]
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
- Return one assessment per pair, in the same order as the pairs.
- This is a relevance check, NOT a compliance decision.

Return only JSON:
{{
  "assessments": [
    {{"relevant": true, "reason": "Brief explanation"}}
  ]
}}

PAIRS:
{chr(10).join(numbered)}
"""

    # Bound decode by pair count; keep headroom for short reasons.
    num_predict = min(256 * len(numbered), 2048)
    try:
        response_text = generate_response(
            prompt,
            num_predict=num_predict,
            timeout=120,
            label="assess_evidence_relevance_batch",
        )
        parsed = json.loads(response_text)
        assessments = parsed.get("assessments") if isinstance(parsed, dict) else None
        if not isinstance(assessments, list) or len(assessments) != len(prepared):
            raise ValueError("Batch assessment count mismatch.")
        parsed_assessments = [
            _parse_relevance_object(item) for item in assessments
        ]
    except (ValueError, json.JSONDecodeError, OSError, requests.RequestException):
        # Safe fallback: same decisions as sequential path, no silent drops.
        return [
            assess_evidence_relevance(requirement, evidence_text)
            for requirement, evidence_text in pairs
        ]

    results = [
        {"relevant": False, "reason": "Evidence text is empty."}
        if index in empty_indexes
        else None
        for index in range(len(pairs))
    ]
    for (index, _requirement, _evidence), assessment in zip(
        prepared, parsed_assessments
    ):
        results[index] = assessment
    return results
