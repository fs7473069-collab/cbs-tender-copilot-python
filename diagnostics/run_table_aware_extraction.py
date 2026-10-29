import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import time
import pymupdf
from app.llm.ollama_client import extract_requirements

path = "data/sample tender.pdf"

with pymupdf.open(path) as doc:
    tables = doc[1].find_tables().tables
    if not tables:
        raise RuntimeError("No table found on page 2")

    table_text = []
    for table in tables:
        rows = table.extract()
        headers = [
            (cell or "").replace("\n", " ").strip()
            for cell in rows[0]
        ]

        for row in rows[1:]:
            values = [
                (cell or "").replace("\n", " ").strip()
                for cell in row
            ]
            if not any(values):
                continue

            table_text.append(
                " | ".join(
                    f"{headers[i]}: {values[i]}"
                    for i in range(min(len(headers), len(values)))
                    if values[i]
                )
            )

formatted_text = "\n".join(table_text)

print("=== STRUCTURED TABLE INPUT ===")
print(formatted_text)

print("\n=== RUNNING ONE EXTRACTION TEST ===")
start = time.perf_counter()
result = extract_requirements({"text": formatted_text})
print(json.dumps(result, indent=2, ensure_ascii=False))
print(f"\nExtraction time: {time.perf_counter() - start:.1f} seconds")
