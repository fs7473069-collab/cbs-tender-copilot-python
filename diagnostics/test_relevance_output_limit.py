import sys
import json
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from app.rag.retriever import TenderRetriever
from app.llm.ollama_client import OLLAMA_URL, MODEL_NAME

RESULTS_PATH = Path(
    "chunks/requirements/output/"
    "90c8579efd9563d4c11664502e06bee02e81f4595b774377dc9dad0afcb340c3.json"
)

SAMPLE_SIZE = 3
MIN_SIMILARITY = 0.55
TEST_NUM_PREDICT = 256


def assess_with_limit(requirement, evidence_text):
    prompt = f"""
You are a strict evidence relevance checker for tender analysis.

Decide whether the evidence substantively discusses the SAME
capability, feature, or obligation as the requirement.

Rules:
- Shared words alone are not enough.
- Do not infer capabilities not described in the passage.
- Relevance does not establish compliance.

Return only JSON:
{{
  "relevant": false,
  "reason": "Brief explanation"
}}

TENDER REQUIREMENT:
{requirement}

CBS EVIDENCE:
{evidence_text}
"""

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL_NAME,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "format": "json",
            "options": {"num_predict": TEST_NUM_PREDICT},
        },
        timeout=300,
    )
    response.raise_for_status()

    data = response.json()
    response_text = data.get("response", "")

    if not isinstance(response_text, str) or not response_text.strip():
        raise ValueError("Ollama returned an empty response.")

    result = json.loads(response_text)

    if (
        not isinstance(result, dict)
        or not isinstance(result.get("relevant"), bool)
        or not isinstance(result.get("reason"), str)
    ):
        raise ValueError("Invalid relevance response structure.")

    return result, data.get("eval_count"), data.get("eval_duration")


def main():
    with RESULTS_PATH.open(encoding="utf-8") as file:
        requirements = json.load(file)

    retriever = TenderRetriever()
    sample = requirements[:SAMPLE_SIZE]

    total_embedding_time = 0.0
    total_relevance_time = 0.0
    assessed = 0
    accepted = 0

    print(f"Testing num_predict={TEST_NUM_PREDICT}")
    print("Production code and baseline files will not be modified.")

    for number, item in enumerate(sample, start=1):
        requirement = item.get("text", "").strip()
        if not requirement:
            continue

        print(f"\nRequirement {number}: {requirement[:100]}")

        start = time.perf_counter()
        candidates = retriever.retrieve(requirement, top_k=3)
        embedding_time = time.perf_counter() - start
        total_embedding_time += embedding_time

        print(f"Embedding + retrieval: {embedding_time:.2f}s")

        for candidate in candidates:
            if candidate["score"] < MIN_SIMILARITY:
                continue

            start = time.perf_counter()
            result, eval_count, eval_duration = assess_with_limit(
                requirement,
                candidate["text"],
            )
            elapsed = time.perf_counter() - start
            total_relevance_time += elapsed
            assessed += 1

            print(
                f"  Similarity={candidate['score']:.3f}; "
                f"time={elapsed:.2f}s; relevant={result['relevant']}"
            )
            print(f"  Reason: {result['reason']}")
            print(f"  Output tokens: {eval_count}")
            print(f"  Generation time reported by Ollama (ns): {eval_duration}")

            if result["relevant"]:
                accepted += 1

    print("\n--- Output-Limit Diagnostic Summary ---")
    print(f"Requirements tested: {len(sample)}")
    print(f"Candidates assessed: {assessed}")
    print(f"Accepted evidence passages: {accepted}")
    print(f"Embedding + retrieval: {total_embedding_time:.2f}s")
    print(f"Relevance checks: {total_relevance_time:.2f}s")
    print(
        "Measured total: "
        f"{total_embedding_time + total_relevance_time:.2f}s"
    )


if __name__ == "__main__":
    main()
