import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.retriever import TenderRetriever

RESULTS_PATH = Path(
    "chunks/requirements/output/"
    "90c8579efd9563d4c11664502e06bee02e81f4595b774377dc9dad0afcb340c3.json"
)

OUTPUT_PATH = Path("diagnostics/retrieval_inspection.json")
SAMPLE_SIZE = 3
TOP_K = 5

with RESULTS_PATH.open(encoding="utf-8") as file:
    requirements = json.load(file)

retriever = TenderRetriever()
report = []

for number, item in enumerate(requirements[:SAMPLE_SIZE], start=1):
    requirement = item.get("text", "").strip()
    if not requirement:
        continue

    print(f"\nRequirement {number}: {requirement}")

    candidates = retriever.retrieve(requirement, top_k=TOP_K)
    entry = {"requirement": requirement, "candidates": []}

    for rank, candidate in enumerate(candidates, start=1):
        record = {
            "rank": rank,
            "similarity": candidate["score"],
            "source": candidate["source"],
            "page": candidate["page"],
            "text": candidate["text"],
        }
        entry["candidates"].append(record)

        print(
            f"\n  Rank {rank} | Similarity: {candidate['score']:.3f}"
            f"\n  Source: {candidate['source']} | Page: {candidate['page']}"
            f"\n  Text: {candidate['text'][:700]}"
        )

    report.append(entry)

OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
OUTPUT_PATH.write_text(
    json.dumps(report, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

print(f"\nInspection saved to: {OUTPUT_PATH}")
print("No Ollama relevance calls were made.")
print("No production code or baseline results were changed.")
