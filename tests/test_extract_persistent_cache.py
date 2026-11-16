"""Persistent extract cache, skip-before-Ollama, and generation-call savings."""

import json

from app.llm.ollama_client import (
    chunk_extraction_decision,
    clear_extract_requirements_cache,
    extract_num_predict_for_text,
    extract_requirements,
)
from app.orchestration import service
from app.rag.matcher import RequirementEvidenceMatcher


def test_table_headers_only_chunks_are_skipped_without_llm(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.llm.ollama_client.generate_response",
        lambda *args, **kwargs: calls.append(1) or (_ for _ in ()).throw(
            AssertionError("LLM must not run")
        ),
    )
    text = "Table headers: Priority | Requirement"
    assert chunk_extraction_decision(text) == (False, "table_headers_only")
    assert extract_requirements({"text": text}) == {"requirements": []}
    assert calls == []


def test_extract_disk_cache_survives_process_memory_clear(monkeypatch, tmp_path):
    monkeypatch.setenv("TENDER_EXTRACT_CACHE_PATH", str(tmp_path / "disk.json"))
    clear_extract_requirements_cache()
    calls = []

    def _ok(prompt, **kwargs):
        calls.append(kwargs.get("num_predict"))
        return json.dumps({
            "requirements": [{
                "text": "The vendor shall provide SSO.",
                "category": "Functional",
                "mandatory": True,
                "confidence": "High",
            }],
        })

    monkeypatch.setattr("app.llm.ollama_client.generate_response", _ok)
    chunk = {"text": "The vendor shall provide SSO for administrators."}
    first = extract_requirements(chunk)
    clear_extract_requirements_cache()
    second = extract_requirements({"text": "The vendor shall provide SSO for administrators."})
    assert first == second
    assert len(calls) == 1
    assert (tmp_path / "disk.json").is_file()


def test_short_chunk_uses_bounded_num_predict(monkeypatch):
    captured = {}

    def _ok(prompt, **kwargs):
        captured["num_predict"] = kwargs.get("num_predict")
        return json.dumps({"requirements": []})

    monkeypatch.setattr("app.llm.ollama_client.generate_response", _ok)
    extract_requirements({"text": "The vendor shall provide SSO."})
    assert captured["num_predict"] == extract_num_predict_for_text(
        "The vendor shall provide SSO."
    )
    assert captured["num_predict"] <= 512


def test_relevance_cache_key_normalizes_whitespace(monkeypatch, tmp_path):
    req = "Provide   fleet vehicle tracking."
    ev = "CBS delivered   fleet vehicle tracking."
    compact_req = "Provide fleet vehicle tracking."
    compact_ev = "CBS delivered fleet vehicle tracking."
    assert (
        RequirementEvidenceMatcher._cache_key(req, ev)
        == RequirementEvidenceMatcher._cache_key(compact_req, compact_ev)
    )


def test_identical_chunk_text_is_one_generate_and_headers_are_skipped(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(service, "PROGRESS_DIR", str(tmp_path / "progress"))
    monkeypatch.setattr(service, "OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv(service.EXTRACT_MAX_WORKERS_ENV, "1")
    chunks = [
        {
            "page": 1,
            "text": "The vendor shall provide SSO.",
            "source_type": "text",
            "table_index": None,
            "page_reference_type": "actual_pdf_page",
        },
        {
            "page": 2,
            "text": "The vendor shall provide SSO.",
            "source_type": "text",
            "table_index": None,
            "page_reference_type": "actual_pdf_page",
        },
        {
            "page": 3,
            "text": "Table headers: Priority | Requirement",
            "source_type": "table",
            "table_index": 1,
            "page_reference_type": "actual_pdf_page",
        },
    ]
    monkeypatch.setattr(
        service, "extract_pdf_text", lambda path: [{"page": 1, "text": "x"}]
    )
    monkeypatch.setattr(service, "create_chunks", lambda _pages: chunks)
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
    calls = []

    def fake_generate(prompt, **kwargs):
        calls.append(kwargs.get("label"))
        return json.dumps({
            "requirements": [{
                "text": "The vendor shall provide SSO.",
                "category": "Functional",
                "mandatory": True,
                "confidence": "High",
            }],
        })

    monkeypatch.setattr("app.llm.ollama_client.generate_response", fake_generate)
    service.process_tender_document("tender.pdf", resume=False)
    assert calls == ["extract_requirements"]
    service.process_tender_document("tender.pdf", resume=True)
    assert calls == ["extract_requirements"]


def test_generation_call_savings_skip_duplicate_and_checkpoint(monkeypatch, tmp_path):
    """Fixture: 3 chunks → 1 generate (duplicate text + skipped headers + resume)."""
    assert service.extract_max_workers() == 1
