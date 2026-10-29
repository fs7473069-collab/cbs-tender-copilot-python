import json
from pathlib import Path
from collections import Counter

path = Path("local_data/cbs_knowledge_base/chunks.json")
chunks = json.loads(path.read_text(encoding="utf-8"))

sources = Counter(
    chunk.get("source", "Unknown source")
    for chunk in chunks
)

print(f"Total chunks: {len(chunks)}")
print(f"Unique sources: {len(sources)}")
print("\nChunks by source:\n")

for source, count in sources.most_common():
    print(f"{count:>4} | {source}")
