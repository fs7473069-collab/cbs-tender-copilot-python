"""Offline call-round count: sequential relevance vs batched rounds.

Does not call Ollama. Uses mocks only.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.rag.matcher import RequirementEvidenceMatcher


def _candidates_for(requirement):
    return [
        {
            "text": f"CBS delivered {requirement.rstrip('.')} for operators.",
            "source": "capability.pdf",
            "page": 1,
            "score": 0.93,
        },
        {
            "text": "Office catering menus and facility booking.",
            "source": "facilities.pdf",
            "page": 2,
            "score": 0.7,
        },
    ]


def main():
    cache = Path(".pytest_cache") / "relevance_batch_bench.json"
    if cache.exists():
        cache.unlink()

    requirements = [
        "Provide fleet vehicle tracking.",
        "Provide single sign-on for administrators.",
        "Provide GPS tracking for logistics.",
        "Provide operator console reporting.",
    ]
    sequential_calls = {"n": 0}
    batch_rounds = {"n": 0, "pairs": 0}

    def assess_one(requirement, evidence):
        sequential_calls["n"] += 1
        return {"relevant": "cbs delivered" in evidence.casefold(), "reason": "offline"}

    def assess_batch(pairs):
        batch_rounds["n"] += 1
        batch_rounds["pairs"] += len(pairs)
        return [
            {
                "relevant": "cbs delivered" in evidence.casefold(),
                "reason": "offline",
            }
            for _requirement, evidence in pairs
        ]

    retriever = type("R", (), {
        "retrieve_many": lambda *args, **kwargs: (
            [_candidates_for(text) for text in requirements],
            [
                {"query_embedding_seconds": 0.01, "retrieval_seconds": 0.001}
                for _ in requirements
            ],
        ),
    })()

    with patch("app.rag.matcher.assess_evidence_relevance", assess_one):
        with patch(
            "app.rag.matcher.assess_evidence_relevance_batch",
            lambda pairs: [assess_one(r, e) for r, e in pairs],
        ):
            # Simulate legacy sequential cost: one assess per requirement.
            matcher = RequirementEvidenceMatcher(
                retriever=retriever, cache_path=cache.with_name("seq.json")
            )
            matcher.match_requirements(requirements, top_k=2)

    if cache.with_name("seq.json").exists():
        cache.with_name("seq.json").unlink()

    with patch("app.rag.matcher.assess_evidence_relevance_batch", assess_batch):
        matcher = RequirementEvidenceMatcher(
            retriever=retriever, cache_path=cache
        )
        matcher.match_requirements(requirements, top_k=2)

    print({
        "requirements": len(requirements),
        "sequential_assess_calls": sequential_calls["n"],
        "batched_rounds": batch_rounds["n"],
        "batched_pairs": batch_rounds["pairs"],
        "expected_round_reduction": (
            sequential_calls["n"] - batch_rounds["n"]
            if sequential_calls["n"]
            else None
        ),
    })


if __name__ == "__main__":
    main()
