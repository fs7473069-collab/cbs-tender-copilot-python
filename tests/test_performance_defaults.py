"""Approved 8B performance defaults: one extract worker, 4-pair batches, packed tables."""

import threading
import time

from app.document.chunker import MAX_CHUNK_CHARS, create_chunks
from app.llm import ollama_client
from app.orchestration import service
from app.orchestration.service import process_tender_document


def test_default_extract_workers_is_one_and_blank_env_uses_default(monkeypatch):
    monkeypatch.delenv(service.EXTRACT_MAX_WORKERS_ENV, raising=False)
    assert service.EXTRACT_MAX_WORKERS_DEFAULT == 1
    assert service.extract_max_workers() == 1
    monkeypatch.setenv(service.EXTRACT_MAX_WORKERS_ENV, "   ")
    assert service.extract_max_workers() == 1
    monkeypatch.setenv(service.EXTRACT_MAX_WORKERS_ENV, "2")
    assert service.extract_max_workers() == 2


def test_default_worker_setting_never_runs_concurrent_extract(
    monkeypatch, tmp_path
):
    monkeypatch.delenv(service.EXTRACT_MAX_WORKERS_ENV, raising=False)
    monkeypatch.setattr(service, "PROGRESS_DIR", str(tmp_path / "progress"))
    monkeypatch.setattr(service, "OUTPUT_DIR", str(tmp_path / "output"))
    chunks = [
        {
            "page": index,
            "text": f"The vendor shall do task {index}.",
            "source_type": "text",
            "table_index": None,
            "page_reference_type": "physical",
        }
        for index in range(1, 6)
    ]
    monkeypatch.setattr(
        service, "extract_pdf_text", lambda path: [{"page": 1, "text": "Tender"}]
    )
    monkeypatch.setattr(service, "create_chunks", lambda pages: chunks)

    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "threads": set()}

    def extract(chunk):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["threads"].add(threading.current_thread().name)
        time.sleep(0.01)
        with lock:
            state["active"] -= 1
        return {
            "requirements": [{
                "text": chunk["text"],
                "category": "Functional",
                "mandatory": True,
                "confidence": "High",
            }]
        }

    monkeypatch.setattr(service, "extract_requirements", extract)
    monkeypatch.setattr(
        service,
        "RequirementEvidenceMatcher",
        lambda: type("M", (), {
            "match_requirements": lambda self, requirements, top_k=3: [
                {"status": "NOT_FOUND", "evidence": [], "note": "test"}
                for _ in requirements
            ],
        })(),
    )

    process_tender_document("tender.pdf")
    assert state["peak"] == 1
    assert state["threads"] == {threading.main_thread().name}


def test_default_relevance_batch_max_pairs_is_four(monkeypatch):
    monkeypatch.delenv("OLLAMA_RELEVANCE_BATCH_MAX_PAIRS", raising=False)
    assert ollama_client.RELEVANCE_BATCH_MAX_PAIRS_DEFAULT == 4
    assert ollama_client.relevance_batch_max_pairs() == 4
    monkeypatch.setenv("OLLAMA_RELEVANCE_BATCH_MAX_PAIRS", "0")
    assert ollama_client.relevance_batch_max_pairs() == 0


def test_default_batch_splits_eight_pairs_into_two_groups_of_four(monkeypatch):
    batch_sizes = []

    def fake_generate(prompt, **kwargs):
        size = sum(
            1 for line in prompt.splitlines() if line.startswith("PAIR ")
        )
        batch_sizes.append(size)
        return __import__("json").dumps({
            "assessments": [
                {"pair_index": i, "relevant": False, "reason": "split"}
                for i in range(size)
            ]
        })

    monkeypatch.delenv("OLLAMA_RELEVANCE_BATCH_MAX_PAIRS", raising=False)
    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(
        ollama_client,
        "assess_evidence_relevance",
        lambda *_: (_ for _ in ()).throw(AssertionError("no sequential")),
    )

    results = ollama_client.assess_evidence_relevance_batch([
        (f"R{i}", f"E{i}") for i in range(8)
    ])
    assert batch_sizes == [4, 4]
    assert len(results) == 8
    assert all(item["relevant"] is False for item in results)


def test_packed_table_chunks_stay_under_limit_and_keep_existing_metadata():
    short_rows = [["Priority", "Requirement"]] + [
        ["M", f"The vendor shall support item {index}."]
        for index in range(6)
    ]
    packed = create_chunks([{
        "page": 3,
        "text": "",
        "page_reference_type": "actual_pdf_page",
        "tables": [{"rows": short_rows}],
    }])
    table_chunks = [chunk for chunk in packed if chunk["source_type"] == "table"]
    assert len(table_chunks) == 1
    text = table_chunks[0]["text"]
    assert len(text) <= MAX_CHUNK_CHARS
    assert text.startswith("Table headers: Priority | Requirement")
    assert "item 0" in text and "item 5" in text
    assert table_chunks[0]["table_index"] == 1
    assert table_chunks[0]["page"] == 3
    assert "table_row_index" not in table_chunks[0]

    wide_row = "x" * 900
    overflowing = create_chunks([{
        "page": 4,
        "text": "",
        "page_reference_type": "actual_pdf_page",
        "tables": [{"rows": [
            ["Col"],
            [wide_row + "A"],
            [wide_row + "B"],
        ]}],
    }])
    split = [chunk for chunk in overflowing if chunk["source_type"] == "table"]
    assert len(split) == 2
    assert all(len(chunk["text"]) <= MAX_CHUNK_CHARS for chunk in split)
    assert wide_row + "A" in split[0]["text"]
    assert wide_row + "B" in split[1]["text"]
    assert wide_row + "B" not in split[0]["text"]
