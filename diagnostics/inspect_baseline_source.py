import json
from pathlib import Path

path = Path(
    "chunks/requirements/output/"
    "90c8579efd9563d4c11664502e06bee02e81f4595b774377dc9dad0afcb340c3.json"
)

with path.open(encoding="utf-8") as file:
    requirements = json.load(file)

print(f"Total saved requirements: {len(requirements)}")

for index, item in enumerate(requirements[:3], start=1):
    source = item.get("source", {})

    print(f"\n{'=' * 70}")
    print(f"Requirement {index}:")
    print(item.get("text", ""))
    print(f"\nSource page: {source.get('page')}")
    print(f"Source chunk: {source.get('chunk')}")
    print("\nOriginal source excerpt:")
    print(source.get("excerpt", "")[:2000])

print("\nInspection complete. No files were modified.")
