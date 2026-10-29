
import sys
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.retriever import TenderRetriever
from app.llm.ollama_client import generate_response

RESULTS_PATH = Path(
    "chunks/requirements/output/"
    "90c8579efd9563d4c11664502e06bee02e81f4595b774377dc9dad0afcb340c3.json"
)

SAMPLE_SIZE = 3
MIN_SIMILARITY = 0.55


def assess_candidates_batched(requirement, candidates):
    passages = [
        {
            "id": i,
            "text": candidate["text"],
        }
        for i, candidate in enumerate(candidates, start=1)
    ]

    prompt = f"""
You are a strict evidence relevance checker.

For each evidence passage, independently decide whether it
substantively discusses the SAME capability, feature, or obligation
as the requirement.

Rules:
- Shared words alone are not enough.
- Do not infer capabilities that the passage does not describe.
- Assess each passage independently.
- Relevance does not establish compliance.
- Return one result for every input passage.
- Use the same IDs as the input.

Return ONLY valid JSON in this exact structure:
{{
  "assessments": [
    {{
      "id": 1,
      "relevant": false,
      "reason": "Brief explanation"
    }}
  ]
}}

REQUIREMENT:
{requirement}

EVIDENCE PASSAGES:
{json.dumps(passages, ensure_ascii=False)}
"""

    result = json.loads(generate_response(prompt))
    assessments = result.get("assessments")

    if not isinstance(assessments, list):
        raise ValueError("Missing assessments list.")

    by_id = {}

    for item in assessments:
        if not isinstance(item, dict):
            raise ValueError("Invalid assessment item.")

        item_id = item.get("id")
        relevant = item.get("relevant")
        reason = item.get("reason")

        if (
            not isinstance(item_id, int)
            or item_id not in range(1, len(candidates) + 1)
            or item_id in by_id
            or not isinstance(relevant, bool)
            or not isinstance(reason, str)
        ):
            raise ValueError("Invalid or duplicate assessment.")

        by_id[item_id] = item

    expected_ids = set(range(1, len(candidates) + 1))

    if set(by_id) != expected_ids:
        raise ValueError("The model did not assess every candidate.")

    return [by_id[i] for i in range(1, len(candidates) + 1)]


def main():
    with RESULTS_PATH.open(encoding="utf-8") as file:
        requirements = json.load(file)

    sample = requirements[:SAMPLE_SIZE]

    retriever = TenderRetriever()

    total_retrieval = 0.0
    total_batch_time = 0.0
    total_candidates = 0
    total_accepted = 0

    for number, item in enumerate(sample, start=1):
        requirement = item.get("text", "").strip()

        if not requirement:
            continue

        print(f"\nRequirement {number}: {requirement[:100]}")

        start = time.perf_counter()
        candidates = retriever.retrieve(requirement, top_k=3)
        retrieval_time = time.perf_counter() - start
        total_retrieval += retrieval_time

        candidates = [
            candidate for candidate in candidates
            if candidate["score"] >= MIN_SIMILARITY
        ]

        print(f"Embedding + retrieval: {retrieval_time:.2f}s")
        print(f"Candidates sent to batch: {len(candidates)}")

        if not candidates:
            continue

        start = time.perf_counter()

        assessments = assess_candidates_batched(
            requirement,
            candidates,
        )

        elapsed = time.perf_counter() - start
        total_batch_time += elapsed
        total_candidates += len(candidates)

        print(f"Batched Qwen call: {elapsed:.2f}s")

        for candidate, assessment in zip(candidates, assessments):
            print(f"  Similarity: {candidate['score']:.3f}")
            print(f"  Relevant: {assessment['relevant']}")
            print(f"  Reason: {assessment['reason']}")

            if assessment["relevant"]:
                total_accepted += 1

    print("\n--- Batched Diagnostic Summary ---")
    print(f"Requirements tested: {len(sample)}")
    print(f"Candidates assessed: {total_candidates}")
    print(f"Accepted evidence passages: {total_accepted}")
    print(f"Embedding + retrieval time: {total_retrieval:.2f}s")
    print(f"Batched relevance time: {total_batch_time:.2f}s")
    print(
        "Measured total stage time: "
        f"{total_retrieval + total_batch_time:.2f}s"
    )
    print("Production code and baseline files were not modified.")


if __name__ == "__main__":
    main()
