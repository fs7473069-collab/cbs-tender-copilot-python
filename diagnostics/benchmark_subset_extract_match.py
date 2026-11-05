"""
Capped live benchmark: extract + production evidence matching on 3 chunks.

Uses current OLLAMA_MODEL (default qwen3:8b) and production helpers:
  - app.llm.ollama_client.extract_requirements
  - app.rag.matcher.RequirementEvidenceMatcher.match_requirements

Safety:
  - Default run hard-capped at exactly 3 inputs; refuses to run if more.
  - Optional ``--smoke-ownership`` runs only the ownership fixture (1 input),
    still refusing any selection larger than MAX_CHUNKS.
  - Does not import messaging consumer/publisher.
  - Does not call process_tender_document.
  - Does not delete or reset caches, progress files, or the knowledge base.

Outputs:
  diagnostics/subset_extract_match_metrics.json
  diagnostics/subset_extract_match_smoke_ownership_metrics.json (smoke mode)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# Running as `python diagnostics/...py` puts diagnostics/ on sys.path[0], not
# the repo root — match other diagnostics that import the app package.
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from app.llm import ollama_client
from app.llm.ollama_client import chunk_extraction_decision
from app.llm.ollama_client import extract_requirements
from app.llm.ollama_client import normalize_chunk_text
from app.llm.ollama_client import relevance_batch_max_pairs
from app.llm.ollama_client import relevance_batch_timeout_seconds
from app.rag.matcher import RequirementEvidenceMatcher

MAX_CHUNKS = 3
SELECTION_FULL = "full"
SELECTION_SMOKE_OWNERSHIP = "smoke_ownership"
OWNERSHIP_FIXTURE = (
    PROJECT_ROOT / "diagnostics" / "fixtures" / "ownership_option_abc_chunk.json"
)
EV_CHUNKS_PATH = PROJECT_ROOT / "chunks" / "ev_tender_chunks.json"
EV_INDEXES = (0, 9)
METRICS_PATH = PROJECT_ROOT / "diagnostics" / "subset_extract_match_metrics.json"
SMOKE_METRICS_PATH = (
    PROJECT_ROOT
    / "diagnostics"
    / "subset_extract_match_smoke_ownership_metrics.json"
)


def parse_selection(argv=None):
    """Parse CLI selection. Default remains the full three-input suite."""
    parser = argparse.ArgumentParser(
        description=(
            "Capped extract+match benchmark. Default: exactly 3 inputs. "
            "Use --smoke-ownership for the ownership fixture only."
        )
    )
    parser.add_argument(
        "--smoke-ownership",
        action="store_true",
        help=(
            "Benchmark-only: run solely diagnostics/fixtures/"
            "ownership_option_abc_chunk.json (1 input). Does not raise "
            f"MAX_CHUNKS={MAX_CHUNKS}."
        ),
    )
    args = parser.parse_args(argv)
    if args.smoke_ownership:
        return SELECTION_SMOKE_OWNERSHIP
    return SELECTION_FULL


def _ownership_input():
    if not OWNERSHIP_FIXTURE.exists():
        raise FileNotFoundError(OWNERSHIP_FIXTURE)
    ownership = json.loads(OWNERSHIP_FIXTURE.read_text(encoding="utf-8"))
    if not isinstance(ownership, dict) or not isinstance(ownership.get("text"), str):
        raise ValueError("Ownership fixture must be an object with a text field.")
    return {
        "id": "ownership_option_abc",
        "source": str(OWNERSHIP_FIXTURE.relative_to(PROJECT_ROOT)),
        "chunk": {
            "text": ownership["text"],
            "page": ownership.get("page"),
            "source_type": ownership.get("source_type"),
            "table_index": ownership.get("table_index"),
            "page_reference_type": ownership.get("page_reference_type"),
        },
    }


def _load_inputs(selection=SELECTION_FULL):
    """Load benchmark inputs for the requested selection (hard-capped)."""
    if selection not in (SELECTION_FULL, SELECTION_SMOKE_OWNERSHIP):
        raise ValueError(f"Unknown selection: {selection}")

    if selection == SELECTION_SMOKE_OWNERSHIP:
        inputs = [_ownership_input()]
        if len(inputs) > MAX_CHUNKS:
            raise RuntimeError(
                f"Refusing to run: {len(inputs)} inputs exceed hard cap "
                f"MAX_CHUNKS={MAX_CHUNKS}."
            )
        if len(inputs) != 1:
            raise RuntimeError(
                "smoke_ownership selection must yield exactly one input."
            )
        return inputs

    if not EV_CHUNKS_PATH.exists():
        raise FileNotFoundError(EV_CHUNKS_PATH)

    ev_chunks = json.loads(EV_CHUNKS_PATH.read_text(encoding="utf-8"))
    if not isinstance(ev_chunks, list):
        raise ValueError("ev_tender_chunks.json must be a JSON list.")
    for index in EV_INDEXES:
        if index < 0 or index >= len(ev_chunks):
            raise IndexError(
                f"EV chunk index {index} out of range for "
                f"{len(ev_chunks)} chunks."
            )
        if not isinstance(ev_chunks[index], dict):
            raise ValueError(f"EV chunk {index} must be an object.")
        if not isinstance(ev_chunks[index].get("text"), str):
            raise ValueError(f"EV chunk {index} must include text.")

    inputs = [
        _ownership_input(),
        {
            "id": "ev_tender_chunk_0",
            "source": f"chunks/ev_tender_chunks.json[{EV_INDEXES[0]}]",
            "chunk": {
                "text": ev_chunks[EV_INDEXES[0]]["text"],
                "page": ev_chunks[EV_INDEXES[0]].get("page"),
                "source_type": ev_chunks[EV_INDEXES[0]].get("source_type"),
            },
        },
        {
            "id": "ev_tender_chunk_9",
            "source": f"chunks/ev_tender_chunks.json[{EV_INDEXES[1]}]",
            "chunk": {
                "text": ev_chunks[EV_INDEXES[1]]["text"],
                "page": ev_chunks[EV_INDEXES[1]].get("page"),
                "source_type": ev_chunks[EV_INDEXES[1]].get("source_type"),
            },
        },
    ]

    if len(inputs) > MAX_CHUNKS:
        raise RuntimeError(
            f"Refusing to run: {len(inputs)} inputs exceed hard cap "
            f"MAX_CHUNKS={MAX_CHUNKS}."
        )
    if len(inputs) != MAX_CHUNKS:
        raise RuntimeError(
            f"Expected exactly {MAX_CHUNKS} inputs, got {len(inputs)}."
        )
    return inputs


def _summarize_evidence(results):
    statuses = {}
    relevance_llm_calls = 0
    relevance_cache_hits = 0
    relevance_llm_seconds = 0.0
    embed_seconds = 0.0
    for item in results or []:
        if not isinstance(item, dict):
            continue
        status = item.get("status") or "UNKNOWN"
        statuses[status] = statuses.get(status, 0) + 1
        timings = item.get("timings") or {}
        relevance_llm_calls += int(timings.get("relevance_llm_calls") or 0)
        relevance_cache_hits += int(timings.get("relevance_cache_hits") or 0)
        relevance_llm_seconds += float(timings.get("relevance_llm_seconds") or 0.0)
        embed_seconds += float(timings.get("query_embedding_seconds") or 0.0)
    return {
        "statuses": statuses,
        "relevance_llm_calls_reported": relevance_llm_calls,
        "relevance_cache_hits_reported": relevance_cache_hits,
        "relevance_llm_seconds_reported": round(relevance_llm_seconds, 3),
        "query_embedding_seconds_reported": round(embed_seconds, 3),
    }


def should_record_batch_fallback(pairs, fallback_calls, max_pairs):
    """Return True only for leaf multi-pair batches that ran sequential work.

    Split parents (``len(pairs) > max_pairs``) must not record: nested leaf
    wrappers already count those sequential generates. Single-pair short
    circuits are not fallbacks.
    """
    if not fallback_calls or fallback_calls < 0:
        return False
    if not isinstance(pairs, list) or len(pairs) <= 1:
        return False
    if isinstance(max_pairs, int) and max_pairs > 0 and len(pairs) > max_pairs:
        return False
    return True


def record_batch_fallback_event(counters, pairs, fallback_calls, max_pairs):
    """Append one fallback event when sequential generates should be counted once."""
    if not should_record_batch_fallback(pairs, fallback_calls, max_pairs):
        return False
    counters["sequential_relevance_from_batch_fallback"] += fallback_calls
    counters["batch_fallbacks"].append({
        "input_pairs": len(pairs),
        "sequential_generate_calls": fallback_calls,
    })
    return True


def main(argv=None):
    selection = parse_selection(argv)
    inputs = _load_inputs(selection=selection)
    metrics_path = (
        SMOKE_METRICS_PATH
        if selection == SELECTION_SMOKE_OWNERSHIP
        else METRICS_PATH
    )

    counters = {
        "generate_by_label": {
            "extract_requirements": 0,
            "assess_evidence_relevance_batch": 0,
            "assess_evidence_relevance": 0,
            "other": 0,
        },
        "extract_skips": [],
        "extract_duplicate_cache_hits": 0,
        "batch_invocations": 0,
        "batch_fallbacks": [],
        "sequential_relevance_from_batch_fallback": 0,
        "sequential_relevance_direct": 0,
    }

    ollama_client.clear_batch_diagnostic_events()

    original_generate = ollama_client.generate_response
    original_extract = extract_requirements
    original_batch = ollama_client.assess_evidence_relevance_batch
    original_single = ollama_client.assess_evidence_relevance

    batch_depth = {"n": 0}

    def counted_generate(prompt, **kwargs):
        label = kwargs.get("label") or "generate"
        if label in counters["generate_by_label"]:
            counters["generate_by_label"][label] += 1
        else:
            counters["generate_by_label"]["other"] += 1
        return original_generate(prompt, **kwargs)

    def counted_extract(chunk):
        text = chunk.get("text", "") if isinstance(chunk, dict) else ""
        should_extract, reason = chunk_extraction_decision(text)
        if not should_extract:
            counters["extract_skips"].append(reason)
            return original_extract(chunk)

        cache_key = normalize_chunk_text(text)
        had_cache = cache_key in ollama_client._EXTRACT_RESULT_CACHE
        gen_before = counters["generate_by_label"]["extract_requirements"]
        result = original_extract(chunk)
        gen_after = counters["generate_by_label"]["extract_requirements"]
        if had_cache and gen_after == gen_before:
            counters["extract_duplicate_cache_hits"] += 1
        return result

    def counted_batch(pairs):
        counters["batch_invocations"] += 1
        batch_depth["n"] += 1
        sequential_before = counters["generate_by_label"]["assess_evidence_relevance"]
        try:
            return original_batch(pairs)
        finally:
            sequential_after = counters["generate_by_label"][
                "assess_evidence_relevance"
            ]
            fallback_calls = sequential_after - sequential_before
            # Leaf multi-pair only: skip split parents (nested wrappers already
            # recorded) and single-pair short-circuits.
            record_batch_fallback_event(
                counters,
                pairs,
                fallback_calls,
                relevance_batch_max_pairs(),
            )
            batch_depth["n"] -= 1

    def counted_single(requirement, evidence_text):
        # Count only callers outside assess_evidence_relevance_batch.
        if batch_depth["n"] == 0:
            counters["sequential_relevance_direct"] += 1
        return original_single(requirement, evidence_text)

    ollama_client.generate_response = counted_generate
    ollama_client.assess_evidence_relevance_batch = counted_batch
    ollama_client.assess_evidence_relevance = counted_single

    # Matcher imports batch/single by name at module load; patch matcher symbols too.
    import app.rag.matcher as matcher_mod

    matcher_mod.assess_evidence_relevance_batch = counted_batch
    matcher_mod.assess_evidence_relevance = counted_single

    print(f"MODEL={ollama_client.MODEL_NAME}", flush=True)
    print(f"MAX_CHUNKS={MAX_CHUNKS}", flush=True)
    print(f"selection={selection}", flush=True)
    print(f"input_count={len(inputs)}", flush=True)
    print(
        "relevance_batch_timeout_s="
        f"{relevance_batch_timeout_seconds()} "
        f"relevance_batch_max_pairs={relevance_batch_max_pairs()}",
        flush=True,
    )
    print(
        "Note: a client Timeout abandons the HTTP wait only; the local Ollama "
        "process may still finish the abandoned generate before accepting new "
        "work.",
        flush=True,
    )
    print("Loading knowledge base (existing index; not rebuilt)...", flush=True)

    # Default constructor loads existing FAISS/metadata and relevance_cache.json.
    matcher = RequirementEvidenceMatcher()

    per_input = []
    wall_started = time.perf_counter()

    try:
        for item in inputs:
            chunk = item["chunk"]
            print(f"\n=== {item['id']} ({item['source']}) ===", flush=True)
            print(
                f"chars={len(chunk.get('text') or '')} "
                f"page={chunk.get('page')} "
                f"source_type={chunk.get('source_type')}",
                flush=True,
            )

            gen_snapshot = {
                label: counters["generate_by_label"][label]
                for label in counters["generate_by_label"]
            }
            skip_before = len(counters["extract_skips"])
            cache_before = counters["extract_duplicate_cache_hits"]
            batch_fb_before = len(counters["batch_fallbacks"])
            diag_before = len(ollama_client.get_batch_diagnostic_events())

            extract_started = time.perf_counter()
            extracted = counted_extract(chunk)
            extract_seconds = time.perf_counter() - extract_started

            requirements = extracted.get("requirements")
            if not isinstance(requirements, list):
                raise ValueError(
                    f"{item['id']}: extract_requirements returned no requirements list."
                )

            match_started = time.perf_counter()
            if requirements:
                evidence_results = matcher.match_requirements(requirements, top_k=3)
            else:
                evidence_results = []
            match_seconds = time.perf_counter() - match_started

            gen_delta = {
                label: counters["generate_by_label"][label] - gen_snapshot[label]
                for label in counters["generate_by_label"]
            }
            evidence_summary = _summarize_evidence(evidence_results)

            record = {
                "id": item["id"],
                "source": item["source"],
                "page": chunk.get("page"),
                "source_type": chunk.get("source_type"),
                "text_chars": len(chunk.get("text") or ""),
                "extract_wall_seconds": round(extract_seconds, 3),
                "match_wall_seconds": round(match_seconds, 3),
                "requirement_count": len(requirements),
                "extract_skips_this_input": counters["extract_skips"][skip_before:],
                "extract_duplicate_cache_hit": (
                    counters["extract_duplicate_cache_hits"] > cache_before
                ),
                "generate_delta": gen_delta,
                "batch_fallbacks_this_input": counters["batch_fallbacks"][
                    batch_fb_before:
                ],
                "batch_diagnostics_this_input": (
                    ollama_client.get_batch_diagnostic_events()[diag_before:]
                ),
                "evidence": evidence_summary,
                "requirement_previews": [
                    (req.get("text") or "")[:120]
                    for req in requirements[:5]
                    if isinstance(req, dict)
                ],
            }
            per_input.append(record)
            print(
                f"extract_s={record['extract_wall_seconds']} "
                f"match_s={record['match_wall_seconds']} "
                f"requirements={record['requirement_count']} "
                f"gen_delta={gen_delta}",
                flush=True,
            )
    finally:
        ollama_client.generate_response = original_generate
        ollama_client.assess_evidence_relevance_batch = original_batch
        ollama_client.assess_evidence_relevance = original_single
        matcher_mod.assess_evidence_relevance_batch = original_batch
        matcher_mod.assess_evidence_relevance = original_single

    wall_seconds = time.perf_counter() - wall_started
    report = {
        "benchmark": "subset_extract_match",
        "selection": selection,
        "model": ollama_client.MODEL_NAME,
        "max_chunks": MAX_CHUNKS,
        "input_count": len(inputs),
        "relevance_batch_timeout_s": relevance_batch_timeout_seconds(),
        "relevance_batch_max_pairs": relevance_batch_max_pairs(),
        "wall_seconds": round(wall_seconds, 3),
        "generate_by_label_total": counters["generate_by_label"],
        "extract_skips": counters["extract_skips"],
        "extract_skip_count": len(counters["extract_skips"]),
        "extract_duplicate_cache_hits": counters["extract_duplicate_cache_hits"],
        "batch_invocations": counters["batch_invocations"],
        "batch_fallbacks": counters["batch_fallbacks"],
        "sequential_relevance_from_batch_fallback": counters[
            "sequential_relevance_from_batch_fallback"
        ],
        "sequential_relevance_direct": counters["sequential_relevance_direct"],
        "batch_diagnostics": ollama_client.get_batch_diagnostic_events(),
        "per_input": per_input,
        "notes": [
            "Does not run process_tender_document or RabbitMQ.",
            "Does not clear progress, KB, or relevance_cache.json.",
            "generate_by_label counts Ollama /api/generate calls by label.",
            "Batch fallback sequential counts are leaf multi-pair generates "
            "labeled assess_evidence_relevance inside "
            "assess_evidence_relevance_batch; split parents are not double-"
            "counted.",
            "batch_diagnostics records assessment_count, parseable_items, "
            "map_status, reused, missing, response_chars, and done_reason "
            "per leaf multi-pair batch generate.",
            "OLLAMA_RELEVANCE_BATCH_TIMEOUT / OLLAMA_RELEVANCE_BATCH_MAX_PAIRS "
            "are read at call time (defaults 120 / 0).",
            "Batch client timeouts leave pairs unassessed; they do not "
            "trigger sequential relevance retries.",
            "After a client Timeout, Ollama may still be busy with the "
            "abandoned generate; later requests can queue behind it.",
            "Use --smoke-ownership for a 1-input ownership-only smoke run.",
        ],
    }

    metrics_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print("\n=== Summary ===", flush=True)
    print(json.dumps({
        "selection": report["selection"],
        "wall_seconds": report["wall_seconds"],
        "generate_by_label_total": report["generate_by_label_total"],
        "extract_skip_count": report["extract_skip_count"],
        "extract_duplicate_cache_hits": report["extract_duplicate_cache_hits"],
        "batch_invocations": report["batch_invocations"],
        "batch_fallback_events": len(report["batch_fallbacks"]),
        "sequential_relevance_from_batch_fallback": report[
            "sequential_relevance_from_batch_fallback"
        ],
        "batch_diagnostic_events": len(report["batch_diagnostics"]),
        "metrics_path": str(metrics_path),
    }, indent=2), flush=True)
    return report


if __name__ == "__main__":
    main()
