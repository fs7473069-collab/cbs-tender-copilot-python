import sys
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.retriever import TenderRetriever
from app.llm.ollama_client import assess_evidence_relevance


RESULTS_PATH = Path(
    "chunks/requirements/output/"
    "90c8579efd9563d4c11664502e06bee02e81f4595b774377dc9dad0afcb340c3.json"
)

SAMPLE_SIZE = 3


def main():
    if not RESULTS_PATH.exists():
        raise FileNotFoundError(f"Baseline results not found: {RESULTS_PATH}")

    with RESULTS_PATH.open(encoding="utf-8") as file:
        requirements = json.load(file)

    sample = requirements[:SAMPLE_SIZE]
    if not sample:
        raise ValueError("No requirements found in the baseline.")

    print("Loading existing knowledge-base index...")
    start = time.perf_counter()
    retriever = TenderRetriever()
    print(f"Retriever initialization: {time.perf_counter() - start:.2f}s")

    total_retrieval = 0.0
    total_relevance = 0.0
    total_candidates = 0
    total_accepted = 0

    for number, item in enumerate(sample, start=1):
        requirement_text = item.get("text", "")
        if not requirement_text:
            print(f"Skipping sample {number}: missing requirement text.")
            continue

        print(f"\nRequirement {number}: {requirement_text[:100]}")

        start = time.perf_counter()
        candidates = retriever.retrieve(requirement_text, top_k=3)
        retrieval_time = time.perf_counter() - start
        total_retrieval += retrieval_time

        print(f"  Embedding + retrieval: {retrieval_time:.2f}s")
        print(f"  Candidates returned: {len(candidates)}")

        for candidate in candidates:
            score = candidate["score"]
            print(f"  Candidate similarity: {score:.3f}")

            if score < 0.55:
                print("  Skipped: below similarity threshold.")
                continue

            total_candidates += 1
            start = time.perf_counter()

            assessment = assess_evidence_relevance(
                requirement_text,
                candidate["text"],
            )

            elapsed = time.perf_counter() - start
            total_relevance += elapsed

            print(f"  Relevance check: {elapsed:.2f}s")
            print(f"  Relevant: {assessment['relevant']}")
            print(f"  Reason: {assessment['reason']}")

            if assessment["relevant"]:
                total_accepted += 1

    print("\n--- Diagnostic Summary ---")
    print(f"Requirements tested: {len(sample)}")
    print(f"Candidates assessed by Qwen: {total_candidates}")
    print(f"Accepted evidence passages: {total_accepted}")
    print(f"Total embedding + retrieval time: {total_retrieval:.2f}s")
    print(f"Total relevance-check time: {total_relevance:.2f}s")
    print(
        f"Measured total stage time: "
        f"{total_retrieval + total_relevance:.2f}s"
    )
    print("\nNo extraction was run and no baseline files were modified.")


if __name__ == "__main__":
    main()
