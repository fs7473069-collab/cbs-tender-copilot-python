import json

import numpy as np
import pytest
import requests

import app.orchestration.service as service
import app.rag.retriever as rag_module
from app.rag.matcher import RequirementEvidenceMatcher
from app.rag.retriever import RetrievalError, TenderRetriever
from app.response.response_builder import format_evidence_match


KB_CHUNKS = [
    {"text": "CBS delivered fleet vehicle tracking with live GPS.", "source": "fleet.pdf", "page": 4, "chunk_index": 7},
    {"text": "CBS maintains security certificates and audits.", "source": "security.docx", "page": 2, "chunk_index": 3},
]


def _fake_embeddings(texts):
    return np.asarray(
        [[1.0, 0.0] if "fleet" in text.lower() else [0.0, 1.0] for text in texts],
        dtype="float32",
    )


@pytest.fixture
def kb_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_module, "KB_DIR", tmp_path)
    monkeypatch.setattr(rag_module, "INDEX_PATH", tmp_path / "cbs.index")
    monkeypatch.setattr(rag_module, "METADATA_PATH", tmp_path / "chunks.json")
    return tmp_path


@pytest.fixture
def relevance(monkeypatch):
    decision = {"relevant": True}

    def assess(requirement, evidence):
        return {"relevant": decision["relevant"], "reason": "test decision"}

    monkeypatch.setattr("app.rag.matcher.assess_evidence_relevance", assess)
    monkeypatch.setattr(
        "app.rag.matcher.assess_evidence_relevance_batch",
        lambda pairs: [assess(r, e) for r, e in pairs],
    )
    return decision


def _persisted_retriever(monkeypatch, kb_paths):
    monkeypatch.setattr(rag_module, "create_embeddings", _fake_embeddings)
    builder = TenderRetriever(persist=False)
    builder.ingest([dict(chunk) for chunk in KB_CHUNKS])
    builder.persist = True
    builder.save()
    return TenderRetriever()


def _matcher(retriever, tmp_path):
    return RequirementEvidenceMatcher(
        retriever=retriever,
        cache_path=tmp_path / "relevance_cache.json",
        min_similarity=0.1,
    )


def test_successful_retrieval_from_persisted_index_keeps_traceability(
    monkeypatch, kb_paths, relevance, tmp_path
):
    retriever = _persisted_retriever(monkeypatch, kb_paths)
    assert retriever.status() == {
        "available": True, "chunks": 2, "dimension": 2, "error_code": None,
    }

    results = _matcher(retriever, tmp_path).match_requirements(
        [{"text": "Provide fleet vehicle tracking."}]
    )

    result = results[0]
    assert result["status"] == "REVIEW_REQUIRED"
    assert result["retrieval"] == {"status": "OK"}
    assert result["timings"]["candidates_retrieved"] == 2
    evidence = result["evidence"][0]
    assert evidence["source"] == "fleet.pdf"
    assert evidence["page"] == 4
    assert evidence["text"] == KB_CHUNKS[0]["text"]
    assert retriever.chunks[0]["chunk_index"] == 7


def test_genuine_no_match_after_successful_search_is_not_found(
    monkeypatch, kb_paths, relevance, tmp_path
):
    relevance["relevant"] = False
    retriever = _persisted_retriever(monkeypatch, kb_paths)

    for result in (
        _matcher(retriever, tmp_path).match_requirement("Provide fleet vehicle tracking."),
        _matcher(retriever, tmp_path).match_requirements(["Provide fleet vehicle tracking."])[0],
    ):
        assert result["status"] == "NOT_FOUND"
        assert result["evidence"] == []
        assert result["retrieval"] == {"status": "OK"}
        assert result["timings"]["candidates_retrieved"] == 2


def test_missing_index_is_not_checked_with_remedy(kb_paths, relevance, tmp_path):
    retriever = TenderRetriever()
    assert retriever.status()["error_code"] == "index_missing"
    matcher = _matcher(retriever, tmp_path)

    batch = matcher.match_requirements(["Provide fleet tracking.", "Provide audits."])
    single = matcher.match_requirement("Provide fleet tracking.")

    for result in batch + [single]:
        assert result["status"] == "NOT_CHECKED"
        assert result["status"] != "NOT_FOUND"
        assert result["evidence"] == []
        assert result["retrieval"]["status"] == "FAILED"
        assert result["retrieval"]["error_code"] == "index_missing"
        assert "python -m app.rag.ingest" in result["note"]
    # Failures are never memoized as a final match result.
    assert matcher._match_memo == {}


def test_corrupt_index_reports_unreadable(kb_paths):
    (kb_paths / "cbs.index").write_bytes(b"not a faiss index")
    (kb_paths / "chunks.json").write_text("[]", encoding="utf-8")

    retriever = TenderRetriever()

    assert retriever.status()["error_code"] == "index_unreadable"
    with pytest.raises(RetrievalError) as raised:
        retriever.retrieve("anything")
    assert raised.value.code == "index_unreadable"


def test_index_metadata_mismatch_does_not_crash_construction(
    monkeypatch, kb_paths
):
    _persisted_retriever(monkeypatch, kb_paths)
    (kb_paths / "chunks.json").write_text(json.dumps(KB_CHUNKS[:1]), encoding="utf-8")

    retriever = TenderRetriever()

    assert retriever.status()["error_code"] == "index_metadata_mismatch"


@pytest.mark.parametrize("failure", [
    requests.ConnectionError("embedding server down"),
    requests.HTTPError("500 Server Error"),
    ValueError("Embeddings contain invalid values."),
])
def test_embedding_failure_is_not_checked(
    monkeypatch, kb_paths, relevance, tmp_path, failure
):
    retriever = _persisted_retriever(monkeypatch, kb_paths)

    def broken(texts):
        raise failure

    monkeypatch.setattr(rag_module, "create_embeddings", broken)
    matcher = _matcher(retriever, tmp_path)

    for result in (
        matcher.match_requirements(["Provide fleet vehicle tracking."])[0],
        matcher.match_requirement("Provide security audits."),
    ):
        assert result["status"] == "NOT_CHECKED"
        assert result["retrieval"]["error_code"] == "embedding_failed"
        assert result["retrieval"]["error_type"] == "RetrievalError"


def test_embedding_dimension_mismatch_is_not_checked(
    monkeypatch, kb_paths, relevance, tmp_path
):
    retriever = _persisted_retriever(monkeypatch, kb_paths)
    monkeypatch.setattr(
        rag_module, "create_embeddings",
        lambda texts: np.ones((len(texts), 3), dtype="float32"),
    )

    result = _matcher(retriever, tmp_path).match_requirements(["Provide fleet tracking."])[0]

    assert result["status"] == "NOT_CHECKED"
    assert result["retrieval"]["error_code"] == "embedding_dimension_mismatch"


def test_unexpected_retriever_exception_is_not_checked(relevance, tmp_path):
    class BrokenRetriever:
        def retrieve(self, *args, **kwargs):
            raise KeyError("boom")

        def retrieve_many(self, *args, **kwargs):
            raise KeyError("boom")

    matcher = _matcher(BrokenRetriever(), tmp_path)
    for result in (
        matcher.match_requirements(["Provide fleet tracking."])[0],
        matcher.match_requirement("Provide fleet tracking."),
    ):
        assert result["status"] == "NOT_CHECKED"
        assert result["retrieval"] == {
            "status": "FAILED",
            "error_code": "retriever_exception",
            "error_type": "KeyError",
        }


def test_failure_logs_do_not_include_requirement_text(kb_paths, relevance, tmp_path, capsys):
    secret_wording = "Confidential tender clause about pricing option Z"
    _matcher(TenderRetriever(), tmp_path).match_requirements([secret_wording])

    out = capsys.readouterr().out
    assert "EVIDENCE_RETRIEVAL status=FAILED code=index_missing" in out
    assert secret_wording not in out


def test_published_evidence_schema_is_unchanged_for_not_checked(
    kb_paths, relevance, tmp_path
):
    result = _matcher(TenderRetriever(), tmp_path).match_requirement("Provide fleet tracking.")

    formatted = format_evidence_match(result)

    assert set(formatted) == {"status", "evidence", "note"}
    assert formatted["status"] == "NOT_CHECKED"


def _orchestration(monkeypatch, tmp_path, extract, matcher_factory):
    monkeypatch.setattr(service, "PROGRESS_DIR", str(tmp_path / "progress"))
    monkeypatch.setattr(service, "OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(service, "extract_pdf_text", lambda path: [{"page": 1, "text": "Tender"}])
    monkeypatch.setattr(service, "create_chunks", lambda pages: [
        {"page": 1, "text": "First chunk"},
        {"page": 2, "text": "Second chunk"},
    ])
    monkeypatch.setattr(service, "extract_requirements", extract)
    monkeypatch.setattr(service, "RequirementEvidenceMatcher", matcher_factory)


def test_matcher_exception_marks_not_checked_not_review_required(monkeypatch, tmp_path):
    class ExplodingMatcher:
        def match_requirements(self, requirements, top_k=3):
            raise RuntimeError("matcher crashed")

    _orchestration(
        monkeypatch, tmp_path,
        lambda chunk: {"requirements": [{"text": f"Req {chunk['text']}"}]},
        ExplodingMatcher,
    )

    result = service.process_tender_document("tender.pdf")

    assert [item["evidence_match"]["status"] for item in result] == ["NOT_CHECKED"] * 2
    assert all(item["evidence_match"]["evidence"] == [] for item in result)
    assert result[0]["source"]["chunk"] == 1


def test_resume_rematches_failed_evidence_without_reextracting(monkeypatch, tmp_path):
    extract_calls = []
    state = {"available": False}

    class Matcher:
        def match_requirements(self, requirements, top_k=3):
            if not state["available"]:
                return [
                    {
                        "status": "NOT_FOUND",
                        "evidence": [],
                        "note": "Evidence matching was not run: Knowledge base is empty.",
                    }
                    for _ in requirements
                ]
            return [
                {
                    "status": "REVIEW_REQUIRED",
                    "evidence": [{"text": "kb", "source": "kb.pdf", "page": 9}],
                    "note": "verify",
                    "retrieval": {"status": "OK"},
                }
                for _ in requirements
            ]

    def extract(chunk):
        extract_calls.append(chunk["text"])
        if chunk["text"] == "Second chunk" and extract_calls.count("Second chunk") == 1:
            raise RuntimeError("Simulated failure")
        return {"requirements": [{"text": f"Req {chunk['text']}"}]}

    _orchestration(monkeypatch, tmp_path, extract, Matcher)
    monkeypatch.setenv(service.EXTRACT_MAX_WORKERS_ENV, "1")

    with pytest.raises(RuntimeError):
        service.process_tender_document("tender.pdf")

    state["available"] = True
    result = service.process_tender_document("tender.pdf")

    assert extract_calls.count("First chunk") == 1
    assert [item["evidence_match"]["status"] for item in result] == [
        "REVIEW_REQUIRED", "REVIEW_REQUIRED",
    ]
    assert result[0]["source"]["chunk"] == 1
    assert result[0]["evidence_match"]["evidence"][0]["page"] == 9
