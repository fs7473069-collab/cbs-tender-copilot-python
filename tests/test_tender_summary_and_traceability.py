import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import app.orchestration.service as service
import app.orchestration.tender_summary as tender_summary
import app.rag.retriever as rag_module
from app.document.structure import describe_chunk_structure, locate_requirement
from app.export.local_export import attach_export_files
from app.llm import ollama_client
from app.rag.matcher import RequirementEvidenceMatcher
from app.rag.retriever import RetrievalError, TenderRetriever
from app.response.response_builder import build_completed_response


PROJECT_ROOT = Path(__file__).resolve().parents[1]

COVER = (
    "REGIONAL HEADQUARTERS F-5/1 ISLAMABAD\n\n"
    "TENDER DOCUMENTS\n"
    "Extension of Existing OFC Network at Pakistan Museum of Natural History.\n"
    "Tender Notice # 002D (b) / (O F C - D e v) / 2 0 2 5 - 2 6\n"
)
SCOPE = (
    "SECTION – I\n"
    "INSTRUCTIONS TO THE BIDDERS\n\n"
    "1.2.1\n"
    "NTC intends to undertake extension of the existing OFC network as per "
    "BOQ attached as Annex-A.\n"
)
DEADLINE = (
    "10.1 EXTENSION IN DEADLINES\n"
    "11. DEADLINE FOR SUBMISSION OF BID\n"
    "11.1 The bid shall be uploaded on or before 1030 Hours on April 28, 2026.\n"
    "13. OPENING OF BID\n"
    "13.1 The committee will download the bids at 1100 Hours on April 28, 2026.\n"
)
PAGES = [
    {"page": 1, "text": COVER},
    {"page": 2, "text": SCOPE},
    {"page": 6, "text": DEADLINE},
]

TOP_LEVEL_KEYS = {
    "schemaVersion", "eventType", "eventId", "organizationId", "workspaceId",
    "jobId", "analysisId", "correlationId", "attempt", "status",
    "completedAt", "processedDocumentIds", "providerName", "modelName",
    "promptVersion", "result",
}
RESULT_KEYS = {
    "title", "customerName", "tenderReference", "submissionDeadline",
    "summary", "extractedMetadata", "requirements",
}
REQUIREMENT_KEYS = {
    "sourceItemKey", "content", "mandatory", "confidence", "sources",
    "evidenceMatch",
}
SOURCE_KEYS = {
    "documentId", "pageNo", "section", "clause", "sheet", "cellRange",
    "excerpt", "sourceRole", "confidence",
}
EVIDENCE_MATCH_KEYS = {"status", "evidence", "note"}
EVIDENCE_ITEM_KEYS = {"text", "source", "page", "similarity"}


def request(tender=None):
    message = {
        "organizationId": "org",
        "workspaceId": "ws",
        "jobId": "job",
        "analysisId": "analysis",
        "correlationId": "corr",
        "documents": [{"documentId": "doc-1"}],
    }
    if tender is not None:
        message["tender"] = tender
    return message


@pytest.fixture
def summary_env(monkeypatch, tmp_path):
    monkeypatch.setattr(tender_summary, "SUMMARY_DIR", str(tmp_path / "summary"))
    monkeypatch.setattr(
        tender_summary, "extract_pdf_text", lambda path: [dict(p) for p in PAGES]
    )
    calls = []
    llm_values = {}

    def fake_llm(text):
        calls.append(text)
        return dict(llm_values)

    monkeypatch.setattr(ollama_client, "extract_tender_summary", fake_llm)
    return calls, llm_values


# Tender summary --------------------------------------------------------------

def test_tender_summary_extracts_supported_values(summary_env):
    calls, llm_values = summary_env
    llm_values.update({
        "title": "Extension of Existing OFC Network at Pakistan Museum of Natural History",
        "customerName": "NTC",
        "summary": "NTC intends to undertake extension of the existing OFC network as per BOQ.",
    })

    result = tender_summary.summarize_tender_document("tender.pdf")

    assert result["tenderReference"] == "002D(b)/(OFC-Dev)/2025-26"
    # The submission deadline, not the later bid-opening time.
    assert result["submissionDeadline"] == "2026-04-28T10:30:00"
    assert result["title"].startswith("Extension of Existing OFC Network")
    assert result["customerName"] == "NTC"
    assert result["summary"].startswith("NTC intends to undertake")
    assert len(calls) == 1
    assert "Tender Notice" in calls[0]


def test_missing_summary_fields_stay_null(summary_env, monkeypatch):
    monkeypatch.setattr(
        tender_summary, "extract_pdf_text",
        lambda path: [{"page": 1, "text": "General conditions apply."}],
    )
    result = tender_summary.summarize_tender_document("tender.pdf")

    assert result == {field: None for field in tender_summary.SUMMARY_FIELDS}
    response = build_completed_response(request(), [], tender_summary=result)
    for field in tender_summary.SUMMARY_FIELDS:
        assert response["result"][field] is None


def test_unsupported_llm_values_are_rejected(summary_env):
    _calls, llm_values = summary_env
    llm_values.update({
        "title": "Supply of Laptops for Karachi Office",
        "customerName": "Pakistan Telecommunication Company Limited",
        "tenderReference": "PTCL-999",
        "submissionDeadline": "May 5, 2026",
        "summary": "NTC will award a contract worth five million rupees.",
    })

    result = tender_summary.summarize_tender_document("tender.pdf")

    assert result["title"] is None
    assert result["customerName"] is None
    assert result["summary"] is None
    # Deterministic values are kept; the invented LLM ones never replace them.
    assert result["tenderReference"] == "002D(b)/(OFC-Dev)/2025-26"
    assert result["submissionDeadline"] == "2026-04-28T10:30:00"


def test_conflicting_deadlines_yield_null():
    text = (
        "Deadline for submission of bids is April 28, 2026. "
        "Bids received later are rejected. " + "x " * 250 +
        "Deadline for submission of bids is May 2, 2026."
    )
    assert tender_summary.find_submission_deadline(text) is None


def test_request_values_take_precedence(summary_env):
    calls, llm_values = summary_env
    llm_values.update({"title": "Extension of Existing OFC Network"})
    tender = {
        "tenderName": "Request Title",
        "customer": "Request Customer",
        "tenderReference": "REQ-1",
        "submissionDeadline": "2026-11-01T12:00:00Z",
        "summary": "Request summary",
    }

    derived = tender_summary.summarize_tender_document(
        "tender.pdf", request_tender=tender
    )
    response = build_completed_response(
        request(tender), [], tender_summary={
            "title": "Document Title", "tenderReference": "DOC-1",
        },
    )

    assert calls == []
    assert derived == {field: None for field in tender_summary.SUMMARY_FIELDS}
    result = response["result"]
    assert result["title"] == "Request Title"
    assert result["customerName"] == "Request Customer"
    assert result["tenderReference"] == "REQ-1"
    assert result["submissionDeadline"] == "2026-11-01T12:00:00Z"
    assert result["summary"] == "Request summary"


def test_document_values_fill_only_missing_request_fields():
    response = build_completed_response(
        request({"tenderName": "Request Title", "customer": "  "}),
        [],
        tender_summary={
            "title": "Document Title",
            "customerName": "NTC",
            "tenderReference": "002D",
            "summary": None,
        },
    )
    result = response["result"]
    assert result["title"] == "Request Title"
    assert result["customerName"] == "NTC"
    assert result["tenderReference"] == "002D"
    assert result["submissionDeadline"] is None
    assert result["summary"] is None


def test_summary_llm_result_is_cached_for_redelivery(summary_env):
    calls, llm_values = summary_env
    llm_values.update({"customerName": "NTC"})

    first = tender_summary.summarize_tender_document("tender.pdf")
    second = tender_summary.summarize_tender_document("tender.pdf")

    assert len(calls) == 1
    assert first == second
    assert second["customerName"] == "NTC"


def test_summary_llm_failure_leaves_fields_null(summary_env, monkeypatch):
    def offline(_text):
        raise ConnectionError("ollama offline")

    monkeypatch.setattr(ollama_client, "extract_tender_summary", offline)
    result = tender_summary.summarize_tender_document("tender.pdf")

    assert result["title"] is None and result["summary"] is None
    assert result["tenderReference"] == "002D(b)/(OFC-Dev)/2025-26"
    assert not os.path.exists(tender_summary.SUMMARY_DIR)


# Metadata ---------------------------------------------------------------------

def test_model_name_comes_from_ollama_model_env():
    script = (
        "import json\n"
        "from app.response.response_builder import build_completed_response\n"
        "r = build_completed_response({'documents': []}, [])\n"
        "print(json.dumps([r['modelName'], r['promptVersion'], r['providerName']]))\n"
    )
    env = dict(os.environ, OLLAMA_MODEL="custom-model:1b")
    output = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()[-1]

    assert json.loads(output) == ["custom-model:1b", "rfp-extraction-v1", "Ollama"]


def test_model_and_prompt_version_use_central_constants(monkeypatch):
    monkeypatch.setattr(ollama_client, "MODEL_NAME", "configured:8b")
    response = build_completed_response(request(), [])

    assert response["modelName"] == "configured:8b"
    assert ollama_client.EXTRACTION_PROMPT_VERSION == "rfp-extraction-v1"
    assert response["promptVersion"] == ollama_client.EXTRACTION_PROMPT_VERSION


# Section and clause -----------------------------------------------------------

def test_section_and_clause_from_numbered_headings():
    chunks = [
        {"page": 2, "source_type": "text", "text": (
            "SECTION – I\n\nINSTRUCTIONS TO THE BIDDERS\n\n"
            "1.1\nThe bidder shall register with PEC.\n\n"
            "1.2\nThe bidder shall provide a bid security of two percent."
        )},
        {"page": 3, "source_type": "text", "text": "Bid security must be valid for 120 days."},
    ]
    structure = describe_chunk_structure(chunks)

    located = locate_requirement(structure[0], "The bidder shall provide a bid security.")
    assert located == {
        "section": "SECTION – I: INSTRUCTIONS TO THE BIDDERS", "clause": "1.2",
    }
    inherited = locate_requirement(structure[1], "Bid security must be valid.")
    assert inherited["clause"] == "1.2"


def test_section_and_clause_null_when_not_established():
    chunks = [
        {"page": 1, "source_type": "text", "text": "TENDER DOCUMENTS\nExtension of network."},
        {"page": 9, "source_type": "text", "text": (
            "Annexure-B\nCOMPLIANCE STATEMENT\n6.\nCountry of Origin\n7.\nPreparation"
        )},
        {"page": 9, "source_type": "text", "text": "Prices shall be as per\nAnnexure-B.\n12\n"},
    ]
    structure = describe_chunk_structure(chunks)

    assert locate_requirement(structure[0], "Extend the network.") == {
        "section": None, "clause": None,
    }
    annex = locate_requirement(structure[1], "State the country of origin.")
    assert annex == {"section": "Annexure-B: COMPLIANCE STATEMENT", "clause": None}
    # A wrapped "Annexure-B." reference and a bare page number change nothing.
    assert locate_requirement(structure[2], "Prices shall be stated.")["clause"] is None


def test_pipeline_publishes_section_and_clause(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "PROGRESS_DIR", str(tmp_path / "progress"))
    monkeypatch.setattr(service, "OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(service, "extract_pdf_text", lambda path: [
        {"page": 1, "text": "TENDER DOCUMENTS"},
        {"page": 2, "text": SCOPE},
    ])
    texts = {
        1: "Tender documents must be read.",
        2: "NTC intends to undertake extension of the existing OFC network.",
    }
    monkeypatch.setattr(service, "extract_requirements", lambda chunk: {
        "requirements": [{
            "text": texts[chunk["page"]],
            "mandatory": False, "confidence": "High",
        }],
    })

    class Matcher:
        def match_requirements(self, requirements, top_k=3):
            return [{"status": "NOT_FOUND", "evidence": [], "note": "n",
                     "retrieval": {"status": "OK"}} for _ in requirements]

    monkeypatch.setattr(service, "RequirementEvidenceMatcher", Matcher)
    monkeypatch.setenv("TENDER_EXTRACT_MAX_WORKERS", "1")

    requirements = service.process_tender_document("tender.pdf")
    sources = [
        item["sources"][0]
        for item in build_completed_response(request(), requirements)["result"]["requirements"]
    ]

    assert (sources[0]["section"], sources[0]["clause"]) == (None, None)
    assert sources[1]["section"] == "SECTION – I: INSTRUCTIONS TO THE BIDDERS"
    assert sources[1]["clause"] == "1.2.1"
    assert sources[1]["pageNo"] == 2
    assert sources[1]["excerpt"] == requirements[1]["source"]["excerpt"]
    assert requirements[1]["source"]["chunk"] == 2


# Evidence traceability and retrieval failures --------------------------------

KB_CHUNKS = [
    {"text": "CBS delivered fleet vehicle tracking with live GPS.", "source": "fleet.pdf", "page": 4},
    {"text": "Tender: bidders shall provide fleet tracking.", "source": "tender_rfp.pdf", "page": 9},
    {"text": "CBS maintains security certificates.", "source": "security.docx", "page": 2},
]


def _fake_embeddings(texts):
    def vector(text):
        lowered = text.lower()
        if "tender" in lowered:
            return [0.9, 0.1]
        return [1.0, 0.0] if "fleet" in lowered else [0.0, 1.0]
    return np.asarray([vector(text) for text in texts], dtype="float32")


@pytest.fixture
def kb(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_module, "KB_DIR", tmp_path)
    monkeypatch.setattr(rag_module, "INDEX_PATH", tmp_path / "cbs.index")
    monkeypatch.setattr(rag_module, "METADATA_PATH", tmp_path / "chunks.json")
    monkeypatch.setattr(rag_module, "create_embeddings", _fake_embeddings)
    monkeypatch.delenv(rag_module.KB_EXCLUDE_SOURCES_ENV, raising=False)
    builder = TenderRetriever(persist=False)
    builder.ingest([dict(chunk) for chunk in KB_CHUNKS])
    builder.persist = True
    builder.save()
    monkeypatch.setattr(
        "app.rag.matcher.assess_evidence_relevance_batch",
        lambda pairs: [{"relevant": True, "reason": "same"} for _ in pairs],
    )
    monkeypatch.setattr(
        "app.rag.matcher.assess_evidence_relevance",
        lambda *_: {"relevant": True, "reason": "same"},
    )
    return tmp_path


def test_evidence_keeps_kb_chunk_id_internally_but_publishes_same_keys(kb, tmp_path):
    matcher = RequirementEvidenceMatcher(
        retriever=TenderRetriever(), cache_path=tmp_path / "cache.json",
        min_similarity=0.1,
    )
    match = matcher.match_requirements(["Provide fleet vehicle tracking."])[0]

    evidence = match["evidence"][0]
    assert evidence["source"] == "fleet.pdf"
    assert evidence["page"] == 4
    assert evidence["kb_chunk_id"] == 0
    stored = json.loads((kb / "chunks.json").read_text())[evidence["kb_chunk_id"]]
    assert stored["text"] == evidence["text"]

    requirement = {
        "text": "Provide fleet vehicle tracking.",
        "source": {"page": 7, "chunk": 3, "excerpt": "Tender chunk excerpt."},
        "evidence_match": match,
    }
    published = build_completed_response(request(), [requirement])
    item = published["result"]["requirements"][0]
    assert set(item["evidenceMatch"]["evidence"][0]) == EVIDENCE_ITEM_KEYS
    assert item["evidenceMatch"]["evidence"][0]["source"] == "fleet.pdf"
    assert item["evidenceMatch"]["evidence"][0]["page"] == 4
    assert item["sources"][0]["pageNo"] == 7
    assert item["sources"][0]["excerpt"] == "Tender chunk excerpt."


def test_source_exclusion_is_opt_in_and_keeps_top_k(kb, monkeypatch):
    default = TenderRetriever().retrieve("fleet tracking", top_k=2)
    assert [item["source"] for item in default] == ["fleet.pdf", "tender_rfp.pdf"]

    monkeypatch.setenv(rag_module.KB_EXCLUDE_SOURCES_ENV, "tender_rfp.pdf")
    retriever = TenderRetriever()
    excluded = retriever.retrieve("fleet tracking", top_k=2)

    assert retriever.active_exclusions() == ["tender_rfp.pdf"]
    assert [item["source"] for item in excluded] == ["fleet.pdf", "security.docx"]
    assert [item["kb_chunk_id"] for item in excluded] == [0, 2]


def test_retrieval_failure_is_published_as_not_checked(tmp_path):
    class Broken:
        def retrieve_many(self, *args, **kwargs):
            raise RetrievalError("index_missing", "missing index")

    matcher = RequirementEvidenceMatcher(
        retriever=Broken(), cache_path=tmp_path / "cache.json"
    )
    match = matcher.match_requirements(["Provide fleet tracking."])[0]
    assert match["retrieval"]["status"] == "FAILED"
    assert service._needs_evidence_rematch({"evidence_match": match})

    published = build_completed_response(
        request(), [{"text": "Provide fleet tracking.", "evidence_match": match}]
    )["result"]["requirements"][0]["evidenceMatch"]
    assert published["status"] == "NOT_CHECKED"
    assert "index_missing" in published["note"]
    assert set(published) == EVIDENCE_MATCH_KEYS


def test_retriever_count_mismatch_is_retrieval_failure(tmp_path):
    class Short:
        def retrieve_many(self, questions, top_k=3, include_metrics=False):
            return [[]], [{}]

    matcher = RequirementEvidenceMatcher(
        retriever=Short(), cache_path=tmp_path / "cache.json"
    )
    results = matcher.match_requirements(["Requirement A.", "Requirement B."])
    assert [r["status"] for r in results] == ["NOT_CHECKED", "NOT_CHECKED"]
    assert results[0]["retrieval"]["error_code"] == "retrieval_count_mismatch"


def test_incomplete_relevance_is_rechecked_on_resume_and_not_memoized(tmp_path, monkeypatch):
    calls = []

    def batch(pairs):
        calls.append(len(pairs))
        return [{"relevant": False, "reason": "timeout", "unassessed": True} for _ in pairs]

    monkeypatch.setattr("app.rag.matcher.assess_evidence_relevance_batch", batch)
    retriever = type("R", (), {"retrieve_many": lambda self, texts, **kw: (
        [[{"text": "CBS delivered fleet tracking.", "source": "a.pdf", "page": 1, "score": 0.9}]
         for _ in texts],
        [{} for _ in texts],
    )})()
    matcher = RequirementEvidenceMatcher(retriever=retriever, cache_path=tmp_path / "c.json")

    first = matcher.match_requirements(["Provide fleet tracking."])[0]
    matcher.match_requirements(["Provide fleet tracking."])

    assert first["status"] == "NOT_CHECKED"
    assert first["retrieval"]["status"] == "OK"
    assert calls == [1, 1]
    assert service._needs_evidence_rematch({"evidence_match": first})
    assert not service._needs_evidence_rematch({"evidence_match": {
        "status": "NOT_FOUND", "evidence": [], "note": "n",
        "retrieval": {"status": "OK"}, "relevance": {"status": "COMPLETE"},
    }})


def test_evidence_preflight_reports_unavailable_index(capsys):
    class Retriever:
        def status(self):
            return {"available": False, "error_code": "index_missing"}

    matcher = type("M", (), {"retriever": Retriever()})()
    status = service.evidence_preflight(matcher)

    assert status["available"] is False
    out = capsys.readouterr().out
    assert "EVIDENCE_PREFLIGHT status=UNAVAILABLE code=index_missing" in out
    assert "NOT_CHECKED" in out


# Regeneration script -----------------------------------------------------------

def test_regeneration_backup_copies_and_rematch_clears_only_evidence(monkeypatch, tmp_path):
    from diagnostics import regenerate_tender_response as regen

    monkeypatch.setattr(regen, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(regen, "BACKUP_ROOT", tmp_path / "backups")
    analysis = tmp_path / "local_data" / "analysis" / "actual_response.json"
    analysis.parent.mkdir(parents=True)
    analysis.write_text('{"eventType": "RFP_ANALYSIS_COMPLETED"}')
    monkeypatch.setattr(regen, "ANALYSIS_RESPONSE", analysis)
    progress = tmp_path / "chunks" / "progress" / "p.json"
    progress.parent.mkdir(parents=True)
    progress.write_text(json.dumps({"completed_chunks": {"1": [
        {"text": "R", "source": {"chunk": 1}, "evidence_match": {"status": "REVIEW_REQUIRED"}},
    ]}}))
    loaded = {
        "progress_path": "chunks/progress/p.json",
        "output_path": "chunks/output/missing.json",
        "document_path": str(tmp_path / "tender.pdf"),
    }
    monkeypatch.chdir(tmp_path)

    backup_dir, copied = regen.backup_results(loaded, "stamp")
    cleared = regen.clear_checkpointed_evidence(loaded)

    assert analysis.exists() and progress.exists()
    assert sorted(copied) == [
        "backups/stamp/chunks/progress/p.json",
        "backups/stamp/local_data/analysis/actual_response.json",
    ]
    backed_up = json.loads((backup_dir / "chunks/progress/p.json").read_text())
    assert "evidence_match" in backed_up["completed_chunks"]["1"][0]
    assert cleared == 1
    item = json.loads(progress.read_text())["completed_chunks"]["1"][0]
    assert item == {"text": "R", "source": {"chunk": 1}}
    assert service._needs_evidence_rematch(item)


def test_regeneration_request_rebuilt_from_previous_response(tmp_path):
    from diagnostics import regenerate_tender_response as regen

    path = tmp_path / "response.json"
    path.write_text(json.dumps({
        "eventType": "RFP_ANALYSIS_COMPLETED", "jobId": "job", "analysisId": "a",
        "organizationId": "org", "workspaceId": "ws", "correlationId": "c",
        "attempt": 1, "processedDocumentIds": ["doc-1"], "result": {"requirements": []},
    }))
    rebuilt = regen._load_request(path)

    assert rebuilt["jobId"] == "job"
    assert rebuilt["documents"] == [{"documentId": "doc-1"}]
    assert rebuilt["tender"] == {}
    assert "result" not in rebuilt


# Published schema --------------------------------------------------------------

def test_published_schema_key_sets_are_unchanged():
    """Builder keys stay the existing analysis contract.

    ``exportFiles`` is a PROPOSED addition for Java review, not an approved
    contract change. ``build_completed_response`` does not add it. The
    analysis consumer attaches it only after both S3 uploads and both
    presigned URLs succeed. See docs/export_contract.md.
    """
    requirement = {
        "text": "The supplier must provide a warranty.",
        "mandatory": True,
        "confidence": "High",
        "source": {
            "page": 4, "chunk": 2, "excerpt": "Warranty clause.",
            "section": "SECTION – I", "clause": "4.2", "confidence": "High",
        },
        "evidence_match": {
            "status": "REVIEW_REQUIRED",
            "evidence": [{
                "text": "CBS warranty.", "source": "a.pdf", "page": 1,
                "similarity": 0.8, "kb_chunk_id": 5, "relevance_reason": "r",
            }],
            "note": "verify",
            "retrieval": {"status": "OK"},
            "relevance": {"status": "COMPLETE", "unassessed": 0},
            "timings": {},
        },
    }
    response = build_completed_response(
        request({"tenderName": "T"}), [requirement],
        tender_summary={"customerName": "NTC"},
    )

    assert set(response) == TOP_LEVEL_KEYS
    assert "exportFiles" not in response
    # PROPOSED for Java review — not an approved contract. Shape only;
    # these URLs are placeholders and are not requested from S3.
    published = attach_export_files(
        response,
        [
            {
                "fileName": "tender-export.xlsx",
                "downloadUrl": "https://download.example.invalid/tender-export.xlsx",
                "path": "/proposed/tender-export.xlsx",
                "bucket": "should-not-appear",
                "key": "should-not-appear",
            },
            {
                "fileName": "tender-export.docx",
                "downloadUrl": "https://download.example.invalid/tender-export.docx",
                "path": "/proposed/tender-export.docx",
                "bucket": "should-not-appear",
                "key": "should-not-appear",
            },
        ],
    )
    assert set(published) == TOP_LEVEL_KEYS | {"exportFiles"}
    assert [item["fileName"] for item in published["exportFiles"]] == [
        "tender-export.xlsx",
        "tender-export.docx",
    ]
    for item in published["exportFiles"]:
        assert set(item) == {"fileName", "downloadUrl"}
    for key in TOP_LEVEL_KEYS:
        assert published[key] == response[key]
    assert set(response["result"]) == RESULT_KEYS
    item = response["result"]["requirements"][0]
    assert set(item) == REQUIREMENT_KEYS
    assert set(item["sources"][0]) == SOURCE_KEYS
    assert set(item["evidenceMatch"]) == EVIDENCE_MATCH_KEYS
    assert set(item["evidenceMatch"]["evidence"][0]) == EVIDENCE_ITEM_KEYS
    assert item["sources"][0]["sourceRole"] == "PRIMARY"
    assert item["sources"][0]["sheet"] is None
    assert item["sources"][0]["cellRange"] is None
    for field in ("title", "customerName", "tenderReference", "submissionDeadline", "summary"):
        assert response["result"][field] is None or isinstance(response["result"][field], str)
    assert response["result"]["extractedMetadata"] == {}
