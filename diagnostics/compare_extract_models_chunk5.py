"""Compare already-installed extraction models on sample-tender chunk 5.

Does not change defaults, progress, caches, or baselines.
"""

from __future__ import annotations

import json
import time

import requests

from app.document.chunker import create_chunks
from app.document.pdf_processor import extract_pdf_text
from app.document.table_extractor import extract_pdf_tables
from app.llm.ollama_client import EXTRACT_NUM_PREDICT, OLLAMA_URL


def chunk5_text() -> str:
    pages = extract_pdf_text(r"data/sample tender.pdf")
    by_page = {}
    for table in extract_pdf_tables(r"data/sample tender.pdf"):
        by_page.setdefault(table["page"], []).append(table)
    for page in pages:
        page["tables"] = by_page.get(page["page"], [])
    return create_chunks(pages)[4]["text"]


def extraction_prompt(tender_text: str) -> str:
    # Keep aligned with app.llm.ollama_client.extract_requirements.
    return f"""
You are a careful tender requirement extraction system.

Extract genuine, explicit requirements from the tender text.

Return ONLY valid JSON. Do not explain or include markdown.

GENERAL RULES:
- Use only information explicitly supported by the source.
- Do not invent requirements, classifications, facts, or obligations.
- Extract complete, standalone requirements.
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
- Use exactly one value: High, Medium, or Low.
- High: the requirement is clearly stated and unambiguous.
- Medium: the requirement is supported by the text but some
  interpretation is needed.
- Low: the statement is ambiguous or its status as a requirement
  is uncertain.
- Confidence describes extraction confidence, not compliance or
  probability of correctness.
- Do not assign High confidence automatically to every requirement.
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


def grounded(requirement: str, source: str) -> bool:
    words = [
        word
        for word in requirement.casefold().replace("/", " ").split()
        if len(word) >= 4
    ]
    if not words:
        return False
    source_lower = source.casefold()
    hits = sum(1 for word in words if word in source_lower)
    return hits / len(words) >= 0.6


def run_model(model: str, prompt: str, source: str) -> dict:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "format": "json",
        "options": {"num_predict": EXTRACT_NUM_PREDICT},
    }
    started = time.perf_counter()
    response = requests.post(OLLAMA_URL, json=payload, timeout=300)
    wall = time.perf_counter() - started
    response.raise_for_status()
    body = response.json()
    raw = body.get("response") or ""

    valid = False
    parse_err = None
    requirements = []
    try:
        parsed = json.loads(raw)
        valid = isinstance(parsed, dict) and isinstance(parsed.get("requirements"), list)
        if valid:
            requirements = [
                item
                for item in parsed["requirements"]
                if isinstance(item, dict)
                and isinstance(item.get("text"), str)
                and item["text"].strip()
            ]
    except json.JSONDecodeError as exc:
        parse_err = str(exc)

    weak = [
        item.get("text", "")
        for item in requirements
        if not grounded(item.get("text", ""), source)
    ]
    eval_count = body.get("eval_count")
    eval_s = (body.get("eval_duration") or 0) / 1e9
    return {
        "model": model,
        "wall_s": round(wall, 2),
        "prompt_eval_s": round((body.get("prompt_eval_duration") or 0) / 1e9, 3),
        "eval_count": eval_count,
        "eval_s": round(eval_s, 2),
        "tok_s": round(eval_count / eval_s, 2) if eval_count and eval_s else None,
        "done_reason": body.get("done_reason"),
        "valid_json": valid,
        "parse_err": parse_err,
        "requirement_count": len(requirements),
        "weak_grounding_count": len(weak),
        "requirements": [item.get("text") for item in requirements],
        "weak_examples": weak[:3],
    }


def main():
    source = chunk5_text()
    prompt = extraction_prompt(source)
    eight = run_model("qwen3:8b", prompt, source)
    four = run_model("qwen3:4b", prompt, source)
    print(
        json.dumps(
            {
                "chunk5_chars": len(source),
                "qwen3_8b": {key: value for key, value in eight.items() if key != "requirements"},
                "qwen3_4b": {key: value for key, value in four.items() if key != "requirements"},
                "qwen3_8b_requirements": eight["requirements"],
                "qwen3_4b_requirements": four["requirements"],
                "qwen3_8b_weak": eight["weak_examples"],
                "qwen3_4b_weak": four["weak_examples"],
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
