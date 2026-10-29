"""Controlled, offline evidence-matching benchmark (no Ollama required)."""

import time
from unittest.mock import patch

from app.rag.matcher import RequirementEvidenceMatcher


REQUIREMENTS = ["Provide fleet vehicle tracking dashboard.", "Provide audit logging capability."]


class ControlledRetriever:
    def retrieve(self, question, top_k=3, include_metrics=False):
        shared = "fleet vehicle tracking dashboard" if "fleet" in question else "audit logging capability"
        candidates = [
            {"text": f"CBS delivered {shared}.", "source": "relevant.pdf", "page": 1, "score": .91},
            {"text": "Encryption certificates and security policies.", "source": "security.pdf", "page": 2, "score": .80},
            {"text": "Office catering and facilities services.", "source": "facilities.pdf", "page": 3, "score": .72},
        ]
        metrics = {"query_embedding_seconds": .012, "retrieval_seconds": .0002}
        return (candidates, metrics) if include_metrics else candidates


def slow_relevance(requirement, evidence):
    time.sleep(.01)  # Deterministic stand-in for a sequential LLM call.
    return {"relevant": True, "reason": "Controlled benchmark match."}


def main():
    candidate_count = len(REQUIREMENTS) * 3
    start = time.perf_counter()
    for requirement in REQUIREMENTS:
        for candidate in ControlledRetriever().retrieve(requirement):
            slow_relevance(requirement, candidate["text"])
    baseline_seconds = time.perf_counter() - start

    start = time.perf_counter()
    with patch("app.rag.matcher.assess_evidence_relevance", slow_relevance):
        matcher = RequirementEvidenceMatcher(
            retriever=ControlledRetriever(),
            cache_path=".pytest_cache/benchmark_relevance_cache.json",
        )
        results = [matcher.match_requirement(requirement) for requirement in REQUIREMENTS]
    optimized_seconds = time.perf_counter() - start

    calls = sum(result["timings"]["relevance_llm_calls"] for result in results)
    cache_hits = sum(result["timings"]["relevance_cache_hits"] for result in results)
    prefiltered = sum(result["timings"]["candidates_prefiltered"] for result in results)
    retrieved = sum(result["timings"]["candidates_retrieved"] for result in results)
    relevance_decisions = calls + cache_hits
    accepted = sum(len(result.get("evidence") or []) for result in results)

    # A zero LLM-call figure is only meaningful if some candidates still reached
    # the relevance stage (live call or cache). Otherwise it is just total filtering.
    if calls == 0 and relevance_decisions == 0:
        raise SystemExit(
            "Benchmark aborted: optimized path made zero relevance decisions "
            "because every candidate was filtered out. That is not a valid "
            "zero-call optimization result."
        )
    if accepted < len(REQUIREMENTS):
        raise SystemExit(
            "Benchmark aborted: relevant controlled evidence was not retained. "
            "Zero/low call counts are not improvements when true matches are dropped."
        )

    print({
        "controlled_requirements": len(REQUIREMENTS),
        "baseline_relevance_calls": candidate_count,
        "optimized_relevance_calls": calls,
        "relevance_cache_hits": cache_hits,
        "candidates_retrieved": retrieved,
        "candidates_prefiltered": prefiltered,
        "candidates_reaching_relevance": relevance_decisions,
        "accepted_relevant_evidence": accepted,
        "baseline_seconds": round(baseline_seconds, 4),
        "optimized_seconds": round(optimized_seconds, 4),
        "query_embedding_seconds_per_requirement": .012,
        "retrieval_seconds_per_requirement": .0002,
        "zero_call_means_all_filtered": False,
    })


if __name__ == "__main__":
    main()
