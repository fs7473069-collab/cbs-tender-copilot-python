"""Offline comparison of relevance call counts: simulated baseline vs current.

Completely offline: mocked relevance only; no Ollama, network, or tender I/O.

The historical matcher that lacked early-stop/memo is not in the repo. The
"simulated_baseline" therefore uses the *current* matcher configured to
approximate pre-optimization behavior:
  - max_accepted_evidence=3 (keep assessing toward multiple accepts)
  - match_requirement per duplicate (no in-run memo across duplicates)
  - single-path assess_evidence_relevance only

"current_implementation" uses production defaults for this matcher:
  - max_accepted_evidence=1 (early-stop after first accept)
  - match_requirements (batch path + in-run memo for duplicate wording)

This reports mocked call counts only. It does not measure or claim wall-clock
speedups.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from app.rag.matcher import RequirementEvidenceMatcher


CANDIDATES = [
    {
        "text": "CBS delivered fleet vehicle tracking dashboard.",
        "source": "fleet.pdf",
        "page": 1,
        "score": 0.95,
    },
    {
        "text": "CBS fleet vehicle tracking is used by logistics teams.",
        "source": "fleet2.pdf",
        "page": 2,
        "score": 0.88,
    },
    {
        "text": "Office catering menus and facility booking.",
        "source": "facilities.pdf",
        "page": 3,
        "score": 0.70,
    },
]

REQUIREMENTS = [
    "Provide fleet vehicle tracking.",
    "Provide fleet vehicle tracking.",
]


def _offline_decision(requirement, evidence):
    return {
        "relevant": "fleet" in evidence.casefold(),
        "reason": "offline mock",
    }


def _make_retriever():
    return type("R", (), {
        "retrieve": lambda *args, **kwargs: (
            list(CANDIDATES),
            {"query_embedding_seconds": 0.01, "retrieval_seconds": 0.001},
        ),
        "retrieve_many": lambda *args, **kwargs: (
            [list(CANDIDATES) for _ in REQUIREMENTS],
            [
                {"query_embedding_seconds": 0.01, "retrieval_seconds": 0.001}
                for _ in REQUIREMENTS
            ],
        ),
    })()


def run_benchmark(cache_dir=None):
    """Run the offline benchmark and return structured call counts."""
    cache_dir = Path(cache_dir) if cache_dir else Path(".pytest_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    baseline_cache = cache_dir / "early_stop_baseline.json"
    current_cache = cache_dir / "early_stop_current.json"
    for path in (baseline_cache, current_cache):
        if path.exists():
            path.unlink()

    counters = {
        "baseline_single_assess_calls": 0,
        "baseline_batch_invocations": 0,
        "baseline_batch_pair_assessments": 0,
        "current_single_assess_calls": 0,
        "current_batch_invocations": 0,
        "current_batch_pair_assessments": 0,
        "network_guard_trips": 0,
    }

    def network_guard(*_args, **_kwargs):
        counters["network_guard_trips"] += 1
        raise AssertionError("Offline benchmark must not perform network I/O.")

    def baseline_assess(requirement, evidence):
        counters["baseline_single_assess_calls"] += 1
        return _offline_decision(requirement, evidence)

    def baseline_batch(pairs):
        counters["baseline_batch_invocations"] += 1
        counters["baseline_batch_pair_assessments"] += len(pairs)
        return [_offline_decision(req, ev) for req, ev in pairs]

    def current_assess(requirement, evidence):
        counters["current_single_assess_calls"] += 1
        return _offline_decision(requirement, evidence)

    def current_batch(pairs):
        counters["current_batch_invocations"] += 1
        counters["current_batch_pair_assessments"] += len(pairs)
        return [_offline_decision(req, ev) for req, ev in pairs]

    retriever = _make_retriever()

    # --- Simulated baseline (not a recovered historical implementation) ---
    with patch("requests.post", network_guard), patch(
        "app.llm.ollama_client.generate_response", network_guard
    ), patch(
        "app.rag.matcher.assess_evidence_relevance", baseline_assess
    ), patch(
        "app.rag.matcher.assess_evidence_relevance_batch", baseline_batch
    ):
        baseline_evidence_counts = []
        for requirement in REQUIREMENTS:
            # Fresh matcher each time: no in-run memo across duplicate wording.
            matcher = RequirementEvidenceMatcher(
                retriever=retriever,
                cache_path=baseline_cache,
                max_accepted_evidence=3,
            )
            # Empty disk cache between duplicates so only memo (absent here)
            # would have helped — baseline intentionally avoids memo reuse.
            if baseline_cache.exists():
                baseline_cache.unlink()
            matcher._cache = {}
            result = matcher.match_requirement(requirement, top_k=3)
            baseline_evidence_counts.append(len(result["evidence"]))

    # --- Current implementation (early-stop + batch path + in-run memo) ---
    with patch("requests.post", network_guard), patch(
        "app.llm.ollama_client.generate_response", network_guard
    ), patch(
        "app.rag.matcher.assess_evidence_relevance", current_assess
    ), patch(
        "app.rag.matcher.assess_evidence_relevance_batch", current_batch
    ):
        matcher = RequirementEvidenceMatcher(
            retriever=retriever,
            cache_path=current_cache,
            max_accepted_evidence=1,
        )
        current_results = matcher.match_requirements(REQUIREMENTS, top_k=3)
        current_evidence_counts = [
            len(item["evidence"]) for item in current_results
        ]

    report = {
        "requirements": len(REQUIREMENTS),
        "candidates_per_requirement": len(CANDIDATES),
        "baseline_kind": "simulated",
        "baseline_note": (
            "Approximates pre-early-stop/memo behavior on the current matcher: "
            "max_accepted_evidence=3, match_requirement per duplicate with "
            "cleared memo/cache. Not a faithful checkout of deleted code."
        ),
        "simulated_baseline": {
            "single_assess_calls": counters["baseline_single_assess_calls"],
            "batch_invocations": counters["baseline_batch_invocations"],
            "batch_pair_assessments": counters[
                "baseline_batch_pair_assessments"
            ],
            "evidence_counts": baseline_evidence_counts,
        },
        "current_implementation": {
            "single_assess_calls": counters["current_single_assess_calls"],
            "batch_invocations": counters["current_batch_invocations"],
            "batch_pair_assessments": counters[
                "current_batch_pair_assessments"
            ],
            "evidence_counts": current_evidence_counts,
        },
        "max_accepted_evidence_note": (
            "Production max_accepted_evidence=1 marks REVIEW_REQUIRED after "
            "the first accepted passage and skips later candidates. That can "
            "omit additional useful supporting evidence (for example the "
            "second fleet passage here) without changing the status. This "
            "benchmark does not change the production setting."
        ),
        "network_guard_trips": counters["network_guard_trips"],
        "mocks_used": True,
    }

    # Hard guarantees for CI / local runs: mocks exercised, no network.
    assert counters["network_guard_trips"] == 0, report
    assert report["mocks_used"] is True
    assert counters["baseline_single_assess_calls"] > 0, report
    assert counters["baseline_batch_invocations"] == 0, report
    assert counters["current_batch_invocations"] > 0, report
    assert counters["current_batch_pair_assessments"] > 0, report
    # Duplicate wording should not double the current batch pair count.
    assert counters["current_batch_pair_assessments"] == 1, report
    assert counters["current_single_assess_calls"] == 0, report
    assert counters["baseline_single_assess_calls"] > counters[
        "current_batch_pair_assessments"
    ], report
    # Document early-stop evidence trade-off without changing production.
    assert baseline_evidence_counts == [2, 2], report
    assert current_evidence_counts == [1, 1], report

    return report


def main():
    report = run_benchmark()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
