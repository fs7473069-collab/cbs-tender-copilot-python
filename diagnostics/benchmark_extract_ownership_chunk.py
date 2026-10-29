"""Extract-only two-run benchmark: same ownership chunk, back-to-back.

Uses production ``extract_requirements`` (same prompt and ``EXTRACT_NUM_PREDICT``).
Run 1 and run 2 use identical model, prompt, settings, and chunk input.
Does not run evidence matching or the full tender pipeline.
Does not modify production code or reduce ``num_predict``.

Outputs:
  diagnostics/extract_ownership_chunk_two_run.json
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJECT_ROOT)

from app.llm import ollama_client
from app.llm.ollama_client import EXTRACT_NUM_PREDICT
from app.llm.ollama_client import MODEL_NAME
from app.llm.ollama_client import extract_requirements
from diagnostics.ownership_extract_quality import check_pricing_engine_ownership

FIXTURE_PATH = (
    PROJECT_ROOT / "diagnostics" / "fixtures" / "ownership_option_abc_chunk.json"
)
OUTPUT_PATH = (
    PROJECT_ROOT / "diagnostics" / "extract_ownership_chunk_two_run.json"
)


def load_chunk():
    if not FIXTURE_PATH.exists():
        raise FileNotFoundError(
            f"Saved ownership chunk not found: {FIXTURE_PATH}"
        )
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Fixture chunk text is empty.")
    lowered = text.casefold()
    if not (
        "option a" in lowered
        and "option b" in lowered
        and "option c" in lowered
    ):
        raise ValueError(
            "Fixture must contain Option A/B/C ownership details."
        )
    return data


def _quality_flags(requirements):
    joined = " ".join(
        item.get("text", "") for item in requirements if isinstance(item, dict)
    ).casefold()
    mandatory_true = sum(
        1 for item in requirements
        if isinstance(item, dict) and item.get("mandatory") is True
    )
    mandatory_false = sum(
        1 for item in requirements
        if isinstance(item, dict) and item.get("mandatory") is False
    )
    return {
        "mentions_option_a": "option a" in joined,
        "mentions_option_b": "option b" in joined,
        "mentions_option_c": "option c" in joined,
        "mentions_all_options": "all options" in joined,
        "requirement_count": len(requirements),
        "mandatory_true_count": mandatory_true,
        "mandatory_false_count": mandatory_false,
        "mandatory_flags": [
            item.get("mandatory")
            for item in requirements
            if isinstance(item, dict)
        ],
    }


def _run_extract(chunk, run_label):
    """Call production extract_requirements once; capture Ollama timing metrics."""
    metrics = {}
    original_log = ollama_client._log_generate_metrics

    def capturing_log(
        label, wall_seconds, body, prompt_chars, num_predict, model=None
    ):
        eval_count = body.get("eval_count")
        eval_duration = body.get("eval_duration")
        prompt_eval_count = body.get("prompt_eval_count")
        prompt_eval_duration = body.get("prompt_eval_duration")
        gen_tok_s = None
        if (
            isinstance(eval_count, int)
            and isinstance(eval_duration, (int, float))
            and eval_duration > 0
        ):
            gen_tok_s = eval_count / (eval_duration / 1e9)
        metrics.clear()
        metrics.update({
            "label": label,
            "model": model or MODEL_NAME,
            "wall_seconds": round(wall_seconds, 3),
            "prompt_chars": prompt_chars,
            "num_predict": num_predict,
            "prompt_eval_count": prompt_eval_count,
            "prompt_eval_seconds": (
                None
                if not prompt_eval_duration
                else round(prompt_eval_duration / 1e9, 3)
            ),
            "eval_count": eval_count,
            "eval_seconds": (
                None if not eval_duration else round(eval_duration / 1e9, 3)
            ),
            "tokens_per_second": (
                None if gen_tok_s is None else round(gen_tok_s, 3)
            ),
            "done_reason": body.get("done_reason"),
            "load_duration_ns": body.get("load_duration"),
            "total_duration_ns": body.get("total_duration"),
            "response_chars": len(body.get("response") or ""),
        })
        return original_log(
            label, wall_seconds, body, prompt_chars, num_predict, model=model
        )

    ollama_client._log_generate_metrics = capturing_log
    try:
        print(f"Starting {run_label}...", flush=True)
        wall_started = time.perf_counter()
        result = extract_requirements(chunk)
        client_wall_seconds = time.perf_counter() - wall_started
    finally:
        ollama_client._log_generate_metrics = original_log

    if not metrics:
        raise RuntimeError(
            f"No Ollama generate metrics for {run_label}; extract may have been skipped."
        )

    requirements = result.get("requirements") if isinstance(result, dict) else None
    if not isinstance(requirements, list):
        raise ValueError(
            f"{run_label}: extract_requirements did not return a requirements list."
        )

    return {
        "run": run_label,
        "client_wall_seconds": round(client_wall_seconds, 3),
        "ollama_metrics": dict(metrics),
        "quality_flags": _quality_flags(requirements),
        "pricing_engine_ownership": check_pricing_engine_ownership(requirements),
        "requirements": requirements,
        "raw_result": result,
    }


def _delta(a, b):
    if a is None or b is None:
        return None
    return round(b - a, 3)


def main():
    fixture = load_chunk()
    chunk = {
        "text": fixture["text"],
        "page": fixture.get("page"),
        "source_type": fixture.get("source_type"),
        "table_index": fixture.get("table_index"),
        "page_reference_type": fixture.get("page_reference_type"),
    }

    run1 = _run_extract(chunk, "run_1")
    run2 = _run_extract(chunk, "run_2")

    m1 = run1["ollama_metrics"]
    m2 = run2["ollama_metrics"]
    q1 = run1["quality_flags"]
    q2 = run2["quality_flags"]
    p1 = run1["pricing_engine_ownership"]
    p2 = run2["pricing_engine_ownership"]

    comparison = {
        "client_wall_seconds": {
            "run_1": run1["client_wall_seconds"],
            "run_2": run2["client_wall_seconds"],
            "delta_run2_minus_run1": _delta(
                run1["client_wall_seconds"], run2["client_wall_seconds"]
            ),
        },
        "prompt_eval_seconds": {
            "run_1": m1.get("prompt_eval_seconds"),
            "run_2": m2.get("prompt_eval_seconds"),
            "delta_run2_minus_run1": _delta(
                m1.get("prompt_eval_seconds"), m2.get("prompt_eval_seconds")
            ),
        },
        "eval_seconds": {
            "run_1": m1.get("eval_seconds"),
            "run_2": m2.get("eval_seconds"),
            "delta_run2_minus_run1": _delta(
                m1.get("eval_seconds"), m2.get("eval_seconds")
            ),
        },
        "eval_count": {
            "run_1": m1.get("eval_count"),
            "run_2": m2.get("eval_count"),
        },
        "tokens_per_second": {
            "run_1": m1.get("tokens_per_second"),
            "run_2": m2.get("tokens_per_second"),
        },
        "requirement_count": {
            "run_1": q1["requirement_count"],
            "run_2": q2["requirement_count"],
        },
        "mandatory_true_count": {
            "run_1": q1["mandatory_true_count"],
            "run_2": q2["mandatory_true_count"],
        },
        "mandatory_false_count": {
            "run_1": q1["mandatory_false_count"],
            "run_2": q2["mandatory_false_count"],
        },
        "mandatory_flags": {
            "run_1": q1["mandatory_flags"],
            "run_2": q2["mandatory_flags"],
        },
        "option_abc_and_all_options_preserved": {
            "run_1": {
                "option_a": q1["mentions_option_a"],
                "option_b": q1["mentions_option_b"],
                "option_c": q1["mentions_option_c"],
                "all_options": q1["mentions_all_options"],
            },
            "run_2": {
                "option_a": q2["mentions_option_a"],
                "option_b": q2["mentions_option_b"],
                "option_c": q2["mentions_option_c"],
                "all_options": q2["mentions_all_options"],
            },
            "both_runs_preserve_all": all([
                q1["mentions_option_a"], q1["mentions_option_b"],
                q1["mentions_option_c"], q1["mentions_all_options"],
                q2["mentions_option_a"], q2["mentions_option_b"],
                q2["mentions_option_c"], q2["mentions_all_options"],
            ]),
            "note": (
                "Heuristic only; does not prove Pricing & product engine "
                "Option A/B/C ownership. See pricing_engine_ownership."
            ),
        },
        "pricing_engine_ownership": {
            "run_1": {
                "passed": p1["passed"],
                "missing_assignments": p1["missing_assignments"],
                "note": p1.get("note"),
            },
            "run_2": {
                "passed": p2["passed"],
                "missing_assignments": p2["missing_assignments"],
                "note": p2.get("note"),
            },
            "both_runs_passed": p1["passed"] and p2["passed"],
        },
    }

    report = {
        "benchmark": "extract_ownership_chunk_two_run",
        "fixture": str(FIXTURE_PATH.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "chunk_meta": {
            "chunk_index_1based": fixture.get("chunk_index_1based"),
            "page": fixture.get("page"),
            "source_type": fixture.get("source_type"),
            "table_index": fixture.get("table_index"),
            "chars": len(fixture["text"]),
            "source_document": fixture.get("source_document"),
        },
        "model": MODEL_NAME,
        "extract_num_predict": EXTRACT_NUM_PREDICT,
        "run_1": run1,
        "run_2": run2,
        "comparison": comparison,
        "notes": [
            "Uses production extract_requirements (prompt + EXTRACT_NUM_PREDICT).",
            "Run 2 starts immediately after run 1 with identical inputs/settings.",
            "No evidence matching; no full tender pipeline.",
            "Output limit not reduced.",
        ],
    }

    OUTPUT_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(json.dumps({
        "saved": str(OUTPUT_PATH),
        "model": MODEL_NAME,
        "extract_num_predict": EXTRACT_NUM_PREDICT,
        "comparison": comparison,
    }, indent=2))


if __name__ == "__main__":
    main()
