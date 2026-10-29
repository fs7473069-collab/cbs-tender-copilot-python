import json
from pathlib import Path

path = Path("local_data/cbs_knowledge_base/chunks.json")
chunks = json.loads(path.read_text(encoding="utf-8"))

keywords = [
    "duplicate tender",
    "existing tender",
    "retry",
    "save failure",
    "inline error",
    "retain entered values",
    "form validation",
]

print(f"Total chunks: {len(chunks)}")
matches = 0

for i, chunk in enumerate(chunks):
    text = chunk.get("text", "")
    if any(word in text.lower() for word in keywords):
        matches += 1
        print(f"\nChunk {i}")
        print(f"Source: {chunk.get('source', 'Unknown source')}")
        print(f"Page: {chunk.get('page', '?')}")
        print(text[:1200])
        print("-" * 60)

print(f"\nTotal matching chunks: {matches}")
