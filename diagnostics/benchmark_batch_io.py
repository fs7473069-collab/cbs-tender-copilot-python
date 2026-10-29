"""Offline benchmark: embedding-call and file-write counts (no Ollama)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.orchestration import service as orchestration
from app.rag.matcher import RequirementEvidenceMatcher
from app.rag.retriever import TenderRetriever
import app.rag.retriever as retriever_module


KB_CHUNKS = [
    {
        "text": "CBS delivered fleet vehicle tracking dashboard.",
        "source": "fleet.pdf",
        "page": 1,
    },
    {
        "text": "CBS maintains audit logging capability.",
        "source": "audit.pdf",
        "page": 2,
    },
    {
        "text": "CBS supports security certificates and policies.",
        "source": "security.pdf",
        "page": 3,
    },
]

REQUIREMENTS = [
    {"text": "Provide fleet vehicle tracking dashboard."},
    {"text": "Provide audit logging capability."},
    {"text": "Provide fleet vehicle tracking dashboard."},
]


def fake_embeddings(texts):
    vectors = []
    for text in texts:
        lower = text.lower()
        if "fleet" in lower:
            vectors.append([1.0, 0.0, 0.0])
        elif "audit" in lower or "logging" in lower:
            vectors.append([0.0, 1.0, 0.0])
        else:
            vectors.append([0.0, 0.0, 1.0])
    return np.asarray(vectors, dtype="float32")


def run_embedding_benchmark():
    embed_calls = []

    def counting_embeddings(texts):
        embed_calls.append(list(texts))
        return fake_embeddings(texts)

    with patch.object(retriever_module, "create_embeddings", counting_embeddings):
        with patch(
            "app.rag.matcher.assess_evidence_relevance",
            lambda requirement, evidence: {
                "relevant": True,
                "reason": "offline benchmark",
            },
        ):
            before_retriever = TenderRetriever(persist=False)
            before_retriever.ingest(KB_CHUNKS)
            before_matcher = RequirementEvidenceMatcher(
                retriever=before_retriever,
                cache_path=Path(".pytest_cache") / "bench_before_cache.json",
                min_similarity=0.1,
            )
            before_start = len(embed_calls)
            for requirement in REQUIREMENTS:
                before_matcher.match_requirement(requirement, top_k=2)
            before_query_batches = embed_calls[before_start:]

            embed_calls.clear()
            after_retriever = TenderRetriever(persist=False)
            after_retriever.ingest(KB_CHUNKS)
            after_matcher = RequirementEvidenceMatcher(
                retriever=after_retriever,
                cache_path=Path(".pytest_cache") / "bench_after_cache.json",
                min_similarity=0.1,
            )
            after_start = len(embed_calls)
            after_matcher.match_requirements(REQUIREMENTS, top_k=2)
            after_query_batches = embed_calls[after_start:]

    return {
        "requirements": len(REQUIREMENTS),
        "before_query_embedding_batches": len(before_query_batches),
        "before_query_embedding_items": sum(len(batch) for batch in before_query_batches),
        "after_query_embedding_batches": len(after_query_batches),
        "after_query_embedding_items": sum(len(batch) for batch in after_query_batches),
        "before_batches": before_query_batches,
        "after_batches": after_query_batches,
    }


def run_write_benchmark(tmp_root: Path):
    if tmp_root.exists():
        for path in sorted(tmp_root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    progress_dir = tmp_root / "progress"
    output_dir = tmp_root / "output"
    cache_path = tmp_root / "relevance_cache.json"
    progress_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    progress_writes = []
    output_writes = []
    cache_writes = []

    original_save_json = orchestration.save_json

    def counting_save_json(path, data):
        normalized = str(path).replace("\\", "/")
        if "/progress/" in normalized:
            progress_writes.append(normalized)
        if "/output/" in normalized:
            output_writes.append(normalized)
        original_save_json(path, data)

    class CountingMatcher(RequirementEvidenceMatcher):
        def _save_cache(self):
            cache_writes.append(1)
            super()._save_cache()

    pages = [
        {"page": 1, "text": "First chunk text"},
        {"page": 2, "text": "Second chunk text"},
    ]
    chunks = [
        {"page": 1, "text": "First chunk text"},
        {"page": 2, "text": "Second chunk text"},
    ]

    def extract(chunk):
        # Use texts that overlap the mocked KB so relevance runs and cache flushes.
        if "First" in chunk["text"]:
            return {
                "requirements": [
                    {"text": "Provide fleet vehicle tracking dashboard."},
                    {"text": "Provide audit logging capability."},
                ]
            }
        return {
            "requirements": [
                {"text": "Provide security certificates."},
                {"text": "Provide fleet vehicle tracking."},
            ]
        }

    def make_matcher():
        retriever = TenderRetriever(persist=False)
        retriever.ingest(KB_CHUNKS)
        return CountingMatcher(
            retriever=retriever,
            cache_path=cache_path,
            min_similarity=0.1,
        )

    chunk_count = len(chunks)
    requirements_per_chunk = 2
    # Historical behavior estimates used for comparison only.
    before_output_writes = chunk_count + 1  # once per chunk + final
    before_cache_writes_upper_bound = chunk_count * requirements_per_chunk * 3

    with patch.object(retriever_module, "create_embeddings", fake_embeddings):
        with patch(
            "app.rag.matcher.assess_evidence_relevance",
            lambda *_: {"relevant": True, "reason": "offline"},
        ):
            with patch.object(orchestration, "PROGRESS_DIR", str(progress_dir)):
                with patch.object(orchestration, "OUTPUT_DIR", str(output_dir)):
                    with patch.object(orchestration, "save_json", counting_save_json):
                        with patch.object(
                            orchestration, "extract_pdf_text", lambda _: pages
                        ):
                            with patch.object(
                                orchestration, "create_chunks", lambda _: chunks
                            ):
                                with patch.object(
                                    orchestration,
                                    "extract_requirements",
                                    extract,
                                ):
                                    with patch.object(
                                        orchestration,
                                        "RequirementEvidenceMatcher",
                                        make_matcher,
                                    ):
                                        orchestration.process_tender_document(
                                            "offline-bench.pdf"
                                        )

    return {
        "chunks": chunk_count,
        "requirements_per_chunk": requirements_per_chunk,
        "progress_writes": len(progress_writes),
        "output_writes": len(output_writes),
        "relevance_cache_writes": len(cache_writes),
        "before_output_writes_estimate": before_output_writes,
        "before_cache_writes_upper_bound_estimate": before_cache_writes_upper_bound,
    }


def main():
    Path(".pytest_cache").mkdir(exist_ok=True)
    embedding = run_embedding_benchmark()
    writes = run_write_benchmark(Path(".pytest_cache") / "batch_io_bench")
    print(json.dumps({"embedding": embedding, "writes": writes}, indent=2))


if __name__ == "__main__":
    main()
