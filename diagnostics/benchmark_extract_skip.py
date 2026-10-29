"""Offline count of extract LLM calls avoided by reference-chunk skip.

Does not call Ollama. Uses saved chunk JSON only.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.llm.ollama_client import chunk_may_contain_requirements
from app.llm.ollama_client import normalize_chunk_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHUNK_FILES = [
    PROJECT_ROOT / "chunks" / "ev_tender_chunks.json",
    PROJECT_ROOT / "chunks" / "cbs_tender_chunks.json",
]


def main():
    for path in CHUNK_FILES:
        if not path.exists():
            print(f"MISSING {path}")
            continue
        chunks = json.loads(path.read_text(encoding="utf-8"))
        skipped = []
        for index, chunk in enumerate(chunks, start=1):
            text = chunk.get("text", "") if isinstance(chunk, dict) else str(chunk)
            if not chunk_may_contain_requirements(text):
                skipped.append((index, normalize_chunk_text(text)[:120]))
        kept = len(chunks) - len(skipped)
        print(f"{path.name}: total={len(chunks)} llm_calls={kept} skipped={len(skipped)}")
        for index, preview in skipped:
            print(f"  skip chunk {index}: {preview}")


if __name__ == "__main__":
    main()
