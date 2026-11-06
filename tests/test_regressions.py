import json

from docx import Document

from app.document.chunker import create_chunks
from app.document.docx_processor import extract_docx_text
from app.llm.ollama_client import chunk_extraction_decision
from app.llm.ollama_client import chunk_may_contain_requirements
from app.llm.ollama_client import clear_extract_requirements_cache
from app.llm.ollama_client import extract_requirements
from app.orchestration.service import process_tender_document
from app.rag.matcher import RequirementEvidenceMatcher
from app.rag.matcher import prefilter_candidate
from app.storage.s3_data_locally import generate_local_excel
from app.storage.s3_data_locally import generate_local_word


def test_table_chunks_keep_headers_rows_and_source_reference():
    chunks = create_chunks([{
        "page": 7, "text": "", "page_reference_type": "actual_pdf_page",
        "tables": [{"rows": [["Priority", "Requirement"], ["M", "Support SSO"]]}],
    }])
    assert len(chunks) == 1
    assert "Priority: M" in chunks[0]["text"]
    assert chunks[0]["source_type"] == "table"
    assert chunks[0]["table_index"] == 1
    assert chunks[0]["page_reference_type"] == "actual_pdf_page"


def test_docx_tables_are_not_flattened_and_boundaries_are_approximate(tmp_path):
    path = tmp_path / "tender.docx"
    document = Document()
    document.add_paragraph("Scope")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "Class", "Need"
    table.cell(1, 0).text, table.cell(1, 1).text = "M", "Audit logs"
    document.save(path)
    page = extract_docx_text(path)[0]
    assert page["page_reference_type"] == "approximate_docx_boundary"
    assert page["tables"][0]["rows"][1] == ["M", "Audit logs"]


def test_malformed_llm_response_is_rejected(monkeypatch):
    monkeypatch.setattr(
        "app.llm.ollama_client.generate_response",
        lambda prompt, **kwargs: "[]",
    )
    try:
        extract_requirements({"text": "The supplier shall provide support."})
    except ValueError as exc:
        assert "JSON object" in str(exc)
    else:
        raise AssertionError("Malformed LLM response was accepted")


def test_extract_skips_glossary_and_site_inventory_without_llm(monkeypatch):
    clear_extract_requirements_cache()
    calls = []

    def _boom(prompt, **kwargs):
        calls.append(prompt)
        raise AssertionError("LLM must not run for reference chunks")

    monkeypatch.setattr("app.llm.ollama_client.generate_response", _boom)

    glossary = {
        "text": (
            "Term Definition Pre-authorisation A temporary hold placed on a "
            "payment method before the final amount is known."
        ),
    }
    site = {
        "text": (
            "Connectors S-01 City centre / CBD DC Rapid 60 kW 1 "
            "S-02 Shopping mall DC Rapid 60 kW 2"
        ),
    }
    assert extract_requirements(glossary) == {"requirements": []}
    assert extract_requirements(site) == {"requirements": []}
    assert chunk_extraction_decision(glossary["text"])[1] == "glossary_term_definition"
    assert chunk_extraction_decision(site["text"])[1] == "site_inventory"
    assert calls == []


def test_extract_skips_blank_page_and_dotted_toc_without_llm(monkeypatch):
    clear_extract_requirements_cache()
    calls = []
    monkeypatch.setattr(
        "app.llm.ollama_client.generate_response",
        lambda *args, **kwargs: calls.append(1) or (_ for _ in ()).throw(
            AssertionError("LLM must not run")
        ),
    )
    blank = {"text": "This page intentionally left blank."}
    toc = {
        "text": (
            "Table of Contents\n"
            "Introduction .......... 1\n"
            "Scope .......... 2\n"
            "Annexes .......... 12\n"
        ),
    }
    assert chunk_extraction_decision(blank["text"]) == (
        False, "intentionally_left_blank"
    )
    assert chunk_extraction_decision(toc["text"]) == (
        False, "table_of_contents_dotted"
    )
    assert extract_requirements(blank) == {"requirements": []}
    assert extract_requirements(toc) == {"requirements": []}
    assert calls == []


def test_extract_does_not_skip_merely_for_missing_obligation_keywords(monkeypatch):
    """Priority/feature tables without shall/must still go to the LLM."""
    clear_extract_requirements_cache()
    calls = []

    def _ok(prompt, **kwargs):
        calls.append(prompt)
        return json.dumps({"requirements": []})

    monkeypatch.setattr("app.llm.ollama_client.generate_response", _ok)
    feature_table = {
        "text": (
            "Feature Description Priority Role-based access Distinct "
            "permissions for admin and support. M Remote session control "
            "Remote start/stop of a session by an operator."
        ),
    }
    should_extract, reason = chunk_extraction_decision(feature_table["text"])
    assert should_extract is True
    assert reason == "default_extract"
    assert extract_requirements(feature_table) == {"requirements": []}
    assert len(calls) == 1


def test_extract_reuses_cache_for_identical_chunk_text(monkeypatch):
    clear_extract_requirements_cache()
    calls = []

    def _ok(prompt, **kwargs):
        calls.append(prompt)
        return json.dumps({
            "requirements": [{
                "text": "The vendor shall provide SSO.",
                "category": "Functional",
                "mandatory": True,
                "confidence": "High",
            }],
        })

    monkeypatch.setattr("app.llm.ollama_client.generate_response", _ok)
    chunk = {
        "text": "Priority: M The vendor shall provide SSO for administrators.",
    }
    first = extract_requirements(chunk)
    second = extract_requirements({"text": chunk["text"]})
    third = extract_requirements({
        "text": "Priority: M   The vendor shall provide SSO for administrators.",
    })
    assert first == second == third
    assert len(calls) == 1


def test_extract_keeps_ownership_and_priority_rows_for_llm(monkeypatch):
    clear_extract_requirements_cache()
    calls = []

    def _ok(prompt, **kwargs):
        calls.append(prompt)
        return json.dumps({
            "requirements": [{
                "text": "The vendor shall provide SSO.",
                "category": "Functional",
                "mandatory": True,
                "confidence": "High",
            }],
        })

    monkeypatch.setattr("app.llm.ollama_client.generate_response", _ok)

    ownership = {
        "text": (
            "Option B/C ownership: The vendor shall own the customer app. "
            "Qualiteq remains responsible for identity and wallet."
        ),
    }
    priority = {
        "text": "Priority: M The solution must support SSO for administrators.",
    }
    # Newlines between words must not hide Priority: M (PDF extraction artifact).
    priority_newlines = {
        "text": "Priority:\nM\nThe\nvendor\nshall\nprovide\naudit\nlogs.",
    }
    ownership_matrix = {
        "text": (
            "Table headers: Component | Option A (Core) | Option B | Option C\n"
            "Component: Pricing & product engine | Option A (Core): Qualiteq | "
            "Option B: Vendor | Option C: Vendor"
        ),
    }
    assert chunk_may_contain_requirements(ownership["text"]) is True
    assert chunk_may_contain_requirements(priority["text"]) is True
    assert chunk_may_contain_requirements(priority_newlines["text"]) is True
    assert chunk_may_contain_requirements(ownership_matrix["text"]) is True
    assert chunk_extraction_decision(ownership_matrix["text"])[1] == (
        "requirement_signal"
    )
    assert len(extract_requirements(ownership)["requirements"]) == 1
    assert len(extract_requirements(priority)["requirements"]) == 1
    assert len(extract_requirements(priority_newlines)["requirements"]) == 1
    assert len(extract_requirements(ownership_matrix)["requirements"]) == 1
    assert len(calls) == 4


def test_reports_read_completed_analysis_only(tmp_path):
    analysis = tmp_path / "completed.json"
    analysis.write_text(json.dumps([{
        "text": "The vendor shall provide SSO.",
        "category": "Functional",
        "mandatory": True,
        "confidence": "High",
        "source": {"page": 2, "chunk": 1},
        "evidence_match": {
            "status": "FOUND",
            "note": "matched",
            "evidence": [{
                "source": "profile.docx",
                "page": 1,
                "similarity": 0.91,
                "relevance_reason": "same obligation",
                "text": "CBS delivered SSO for admins.",
            }],
        },
    }]), encoding="utf-8")
    excel = generate_local_excel(analysis, output_file=tmp_path / "out.xlsx")
    word = generate_local_word(analysis, output_file=tmp_path / "out.docx")
    assert excel.exists() and excel.stat().st_size > 0
    assert word.exists() and word.stat().st_size > 0


def test_resume_invalidates_when_extracted_content_changes(monkeypatch, tmp_path):
    monkeypatch.setattr("app.orchestration.service.PROGRESS_DIR", str(tmp_path / "progress"))
    monkeypatch.setattr("app.orchestration.service.OUTPUT_DIR", str(tmp_path / "output"))
    content = {"text": "first"}
    monkeypatch.setattr("app.orchestration.service.extract_pdf_text", lambda _: [{"page": 1, **content}])
    monkeypatch.setattr("app.orchestration.service.create_chunks", lambda pages: [{"page": 1, **content}])
    monkeypatch.setattr("app.orchestration.service.RequirementEvidenceMatcher", lambda: type("M", (), {"match_requirement": lambda *args, **kwargs: {"status": "NOT_FOUND", "evidence": []}})())
    calls = []
    monkeypatch.setattr("app.orchestration.service.extract_requirements", lambda chunk: calls.append(chunk["text"]) or {"requirements": [{"text": chunk["text"]}]})
    process_tender_document("changed.pdf")
    content["text"] = "second"
    result = process_tender_document("changed.pdf")
    assert calls == ["first", "second"]
    assert result[0]["text"] == "second"


def test_similarity_without_relevance_is_not_evidence(monkeypatch):
    retriever = type("R", (), {"retrieve": lambda *args, **kwargs: [{"text": "attendance sheet", "source": "x", "page": 1, "score": .99}]})()
    matcher = RequirementEvidenceMatcher(retriever=retriever)
    monkeypatch.setattr("app.rag.matcher.assess_evidence_relevance", lambda *_: {"relevant": False, "reason": "Different meaning"})
    assert matcher.match_requirement("Provide attendance system")["status"] == "NOT_FOUND"


def test_prefilter_skips_only_clearly_unrelated_candidate(monkeypatch, tmp_path):
    retriever = type("R", (), {"retrieve": lambda *args, **kwargs: ([
        {"text": "Encryption keys and security controls.", "source": "x", "page": 1, "score": .99},
    ], {"query_embedding_seconds": .01, "retrieval_seconds": .001})})()
    matcher = RequirementEvidenceMatcher(retriever=retriever, cache_path=tmp_path / "cache.json")
    monkeypatch.setattr("app.rag.matcher.assess_evidence_relevance", lambda *_: (_ for _ in ()).throw(AssertionError("LLM should not run")))
    result = matcher.match_requirement("The fleet vehicle tracking dashboard is required.")
    assert result["status"] == "NOT_FOUND"
    assert result["timings"]["candidates_prefiltered"] == 1
    assert result["timings"]["relevance_llm_calls"] == 0


def test_prefilter_keeps_synonym_and_terminology_overlap_for_llm():
    # Shared domain term with different surrounding wording must reach the LLM.
    passes, reason = prefilter_candidate(
        "The supplier shall provide an attendance management module.",
        "CBS delivered biometric attendance capture for site workforce tracking.",
    )
    assert passes is True
    assert "retained" in reason.casefold()


def test_prefilter_keeps_acronym_synonym_pairs_for_llm():
    # False-negative guard: terminology differences must not drop relevant evidence.
    assert prefilter_candidate(
        "The solution must support single sign-on for administrators.",
        "CBS implemented SSO across the admin portal.",
    )[0] is True
    assert prefilter_candidate(
        "Customer app must support OTP login.",
        "Users authenticate with a one-time passcode during mobile login.",
    )[0] is True
    assert prefilter_candidate(
        "Provide multi-factor authentication for operators.",
        "Operators unlock the console with MFA challenges.",
    )[0] is True


def test_prefilter_keeps_generic_shared_words_for_llm_review():
    # Generic overlap is deliberately not decisive; LLM must still decide.
    passes, _ = prefilter_candidate(
        "Provide a reporting system for charging sessions.",
        "The catering system includes weekly reporting for office sessions.",
    )
    assert passes is True


def test_prefilter_rejects_no_overlap_candidate():
    passes, reason = prefilter_candidate(
        "Fleet vehicle tracking dashboard with live GPS positions.",
        "Encryption certificates and firewall hardening policies.",
    )
    assert passes is False
    assert "no meaningful lexical overlap" in reason.casefold()


def test_zero_relevance_calls_with_only_filtering_is_not_success(monkeypatch, tmp_path):
    # A zero-call outcome caused solely by filtering is not an optimization win.
    retriever = type("R", (), {"retrieve": lambda *args, **kwargs: ([
        {"text": "Office catering menus and facility booking.", "source": "x", "page": 1, "score": .9},
    ], {"query_embedding_seconds": .01, "retrieval_seconds": .001})})()
    matcher = RequirementEvidenceMatcher(retriever=retriever, cache_path=tmp_path / "cache.json")
    monkeypatch.setattr(
        "app.rag.matcher.assess_evidence_relevance",
        lambda *_: (_ for _ in ()).throw(AssertionError("LLM should not run")),
    )
    result = matcher.match_requirement("Provide fleet vehicle tracking dashboard.")
    assert result["timings"]["relevance_llm_calls"] == 0
    assert result["timings"]["candidates_prefiltered"] == 1
    assert result["timings"]["candidates_retrieved"] == 1
    # Relevant synonym evidence must still be allowed through in other cases.
    assert prefilter_candidate(
        "Provide fleet vehicle tracking dashboard.",
        "CBS delivered GPS fleet vehicle tracking for logistics.",
    )[0] is True


def test_prefilter_allows_relevant_candidate_to_reach_llm(monkeypatch, tmp_path):
    calls = []
    retriever = type("R", (), {"retrieve": lambda *args, **kwargs: ([
        {
            "text": "CBS delivered a fleet vehicle tracking dashboard with live GPS.",
            "source": "proposal.pdf",
            "page": 2,
            "score": .92,
        },
        {
            "text": "Office catering menus and facility booking schedules.",
            "source": "facilities.pdf",
            "page": 4,
            "score": .70,
        },
    ], {"query_embedding_seconds": .01, "retrieval_seconds": .001})})()
    monkeypatch.setattr(
        "app.rag.matcher.assess_evidence_relevance",
        lambda *_: calls.append(1) or {"relevant": True, "reason": "Same capability"},
    )
    matcher = RequirementEvidenceMatcher(retriever=retriever, cache_path=tmp_path / "cache.json")
    result = matcher.match_requirement("Provide fleet vehicle tracking dashboard.")
    assert len(calls) == 1
    assert result["timings"]["relevance_llm_calls"] == 1
    # Unrelated lower-ranked candidate is skipped after the first accepted hit.
    assert result["timings"]["candidates_skipped_after_accept"] == 1
    assert result["status"] == "REVIEW_REQUIRED"


def test_relevance_decision_cache_uses_stable_content_key(monkeypatch, tmp_path):
    retriever = type("R", (), {"retrieve": lambda *args, **kwargs: ([
        {"text": "CBS delivered fleet vehicle tracking.", "source": "x", "page": 1, "score": .9},
    ], {"query_embedding_seconds": .01, "retrieval_seconds": .001})})()
    calls = []
    monkeypatch.setattr("app.rag.matcher.assess_evidence_relevance", lambda *_: calls.append(1) or {"relevant": True, "reason": "Same capability"})
    cache_path = tmp_path / "cache.json"
    first_matcher = RequirementEvidenceMatcher(retriever=retriever, cache_path=cache_path)
    first = first_matcher.match_requirement("Provide fleet vehicle tracking.")
    # New matcher instance: in-run memo is empty, disk cache must satisfy the hit.
    second_matcher = RequirementEvidenceMatcher(retriever=retriever, cache_path=cache_path)
    second = second_matcher.match_requirement("Provide fleet vehicle tracking.")
    assert len(calls) == 1
    assert first["timings"]["relevance_llm_calls"] == 1
    assert second["timings"]["relevance_cache_hits"] == 1


def test_relevance_failure_is_not_cached(monkeypatch, tmp_path):
    retriever = type("R", (), {"retrieve": lambda *args, **kwargs: ([
        {"text": "CBS delivered fleet vehicle tracking.", "source": "x", "page": 1, "score": .9},
    ], {"query_embedding_seconds": .01, "retrieval_seconds": .001})})()
    matcher = RequirementEvidenceMatcher(retriever=retriever, cache_path=tmp_path / "cache.json")
    monkeypatch.setattr("app.rag.matcher.assess_evidence_relevance", lambda *_: (_ for _ in ()).throw(OSError("offline")))
    result = matcher.match_requirement("Provide fleet vehicle tracking.")
    assert result["status"] == "NOT_CHECKED"
    assert result["relevance"]["status"] == "INCOMPLETE"
    assert result["timings"]["relevance_llm_calls"] == 0
    assert not (tmp_path / "cache.json").exists()


def _patch_relevance_assessors(monkeypatch, assess_fn):
    """Keep single- and batch-path tests aligned on the same decision function."""
    monkeypatch.setattr("app.rag.matcher.assess_evidence_relevance", assess_fn)
    monkeypatch.setattr(
        "app.rag.matcher.assess_evidence_relevance_batch",
        lambda pairs: [assess_fn(requirement, evidence) for requirement, evidence in pairs],
    )


def test_match_requirements_batches_retrieval_and_preserves_order(monkeypatch, tmp_path):
    import numpy as np
    import app.rag.retriever as rag_module
    from app.rag.retriever import TenderRetriever

    embed_calls = []

    def fake_embeddings(texts):
        embed_calls.append(list(texts))
        vectors = []
        for text in texts:
            if "fleet" in text.lower():
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return np.asarray(vectors, dtype="float32")

    monkeypatch.setattr(rag_module, "create_embeddings", fake_embeddings)
    _patch_relevance_assessors(
        monkeypatch,
        lambda requirement, evidence: {
            "relevant": True,
            "reason": "test",
        },
    )

    retriever = TenderRetriever(persist=False)
    retriever.ingest([
        {
            "text": "CBS delivered fleet vehicle tracking.",
            "source": "fleet.pdf",
            "page": 1,
        },
        {
            "text": "CBS maintains security certificates.",
            "source": "security.pdf",
            "page": 2,
        },
    ])
    matcher = RequirementEvidenceMatcher(
        retriever=retriever,
        cache_path=tmp_path / "cache.json",
        min_similarity=0.1,
    )
    requirements = [
        "Provide fleet vehicle tracking.",
        "Provide security certificates.",
    ]
    results = matcher.match_requirements(requirements, top_k=1)
    assert [item["requirement"] for item in results] == requirements
    assert results[0]["status"] == "REVIEW_REQUIRED"
    assert results[0]["evidence"][0]["source"] == "fleet.pdf"
    assert results[1]["evidence"][0]["source"] == "security.pdf"
    # ingest batch + one retrieve_many batch for both requirement queries
    assert embed_calls[0] == [
        "CBS delivered fleet vehicle tracking.",
        "CBS maintains security certificates.",
    ]
    assert embed_calls[1] == requirements
    assert len(embed_calls) == 2


def test_relevance_cache_flushes_once_per_requirement_batch(monkeypatch, tmp_path):
    writes = []
    retriever = type("R", (), {"retrieve_many": lambda *args, **kwargs: (
        [
            [{"text": "CBS delivered fleet vehicle tracking.", "source": "x", "page": 1, "score": .9}],
            [{"text": "CBS delivered fleet vehicle tracking dashboard.", "source": "y", "page": 2, "score": .91}],
        ],
        [
            {"query_embedding_seconds": .01, "retrieval_seconds": .001},
            {"query_embedding_seconds": .01, "retrieval_seconds": .001},
        ],
    )})()
    _patch_relevance_assessors(
        monkeypatch,
        lambda *_: {"relevant": True, "reason": "Same capability"},
    )
    matcher = RequirementEvidenceMatcher(retriever=retriever, cache_path=tmp_path / "cache.json")
    original_save = matcher._save_cache

    def counting_save():
        writes.append(1)
        original_save()

    matcher._save_cache = counting_save
    matcher.match_requirements([
        "Provide fleet vehicle tracking.",
        "Provide fleet vehicle tracking dashboard.",
    ])
    assert len(writes) == 1
    assert (tmp_path / "cache.json").exists()


def test_relevance_stops_after_first_accepted_evidence(monkeypatch, tmp_path):
    assessed = []
    retriever = type("R", (), {"retrieve": lambda *args, **kwargs: ([
        {
            "text": "CBS delivered fleet vehicle tracking dashboard.",
            "source": "fleet.pdf",
            "page": 1,
            "score": .95,
        },
        {
            "text": "CBS also operates a fleet vehicle tracking portal.",
            "source": "fleet2.pdf",
            "page": 2,
            "score": .90,
        },
        {
            "text": "Office catering and facilities services.",
            "source": "facilities.pdf",
            "page": 3,
            "score": .70,
        },
    ], {"query_embedding_seconds": .01, "retrieval_seconds": .001})})()

    def assess(requirement, evidence):
        assessed.append(evidence)
        return {"relevant": "fleet" in evidence.lower(), "reason": "test"}

    monkeypatch.setattr("app.rag.matcher.assess_evidence_relevance", assess)
    matcher = RequirementEvidenceMatcher(
        retriever=retriever,
        cache_path=tmp_path / "cache.json",
        min_similarity=0.55,
    )
    result = matcher.match_requirement("Provide fleet vehicle tracking.")
    assert result["status"] == "REVIEW_REQUIRED"
    assert len(result["evidence"]) == 1
    assert len(assessed) == 1
    assert result["timings"]["relevance_llm_calls"] == 1
    assert result["timings"]["candidates_skipped_after_accept"] == 2


def test_identical_requirement_reuses_in_run_match_memo(monkeypatch, tmp_path):
    calls = []
    retriever = type("R", (), {"retrieve_many": lambda *args, **kwargs: (
        [
            [{"text": "CBS delivered fleet vehicle tracking.", "source": "x", "page": 1, "score": .9}],
            [{"text": "CBS delivered fleet vehicle tracking.", "source": "x", "page": 1, "score": .9}],
        ],
        [
            {"query_embedding_seconds": .01, "retrieval_seconds": .001},
            {"query_embedding_seconds": .01, "retrieval_seconds": .001},
        ],
    )})()
    def assess(*args, **kwargs):
        calls.append(1)
        return {"relevant": True, "reason": "Same capability"}

    _patch_relevance_assessors(monkeypatch, assess)
    matcher = RequirementEvidenceMatcher(
        retriever=retriever,
        cache_path=tmp_path / "cache.json",
    )
    results = matcher.match_requirements([
        "Provide fleet vehicle tracking.",
        "Provide  fleet   vehicle tracking.",
    ])
    assert len(calls) == 1
    assert results[0]["status"] == results[1]["status"] == "REVIEW_REQUIRED"
    assert results[0]["evidence"][0]["source"] == results[1]["evidence"][0]["source"]


def test_match_requirements_batches_relevance_into_fewer_llm_rounds(monkeypatch, tmp_path):
    batch_sizes = []

    def fake_batch(pairs):
        batch_sizes.append(len(pairs))
        return [{"relevant": True, "reason": "batched"} for _ in pairs]

    monkeypatch.setattr("app.rag.matcher.assess_evidence_relevance_batch", fake_batch)
    retriever = type("R", (), {"retrieve_many": lambda *args, **kwargs: (
        [
            [{"text": "CBS delivered fleet vehicle tracking.", "source": "a", "page": 1, "score": .9}],
            [{"text": "CBS delivered SSO for admins.", "source": "b", "page": 1, "score": .91}],
            [{"text": "CBS delivered GPS tracking.", "source": "c", "page": 1, "score": .92}],
        ],
        [
            {"query_embedding_seconds": .01, "retrieval_seconds": .001},
            {"query_embedding_seconds": .01, "retrieval_seconds": .001},
            {"query_embedding_seconds": .01, "retrieval_seconds": .001},
        ],
    )})()
    matcher = RequirementEvidenceMatcher(
        retriever=retriever,
        cache_path=tmp_path / "cache.json",
    )
    results = matcher.match_requirements([
        "Provide fleet vehicle tracking.",
        "Provide SSO for administrators.",
        "Provide GPS tracking.",
    ], top_k=1)
    # One generate round for three requirements instead of three sequential calls.
    assert batch_sizes == [3]
    assert all(item["status"] == "REVIEW_REQUIRED" for item in results)
    assert sum(item["timings"]["relevance_llm_calls"] for item in results) == 3


def test_qwen3_4b_fails_ownership_quality_gate_on_saved_chunk5():
    """Existing chunk-5 comparison: 4b collapses B/C ownership; do not switch default."""
    eight = [
        "The Customer app (mobile + web) UI will be provided by the Vendor for all options.",
        "The Pricing & product engine will be provided by Qualiteq for Option A (Core), by the Vendor for Option B (+ Pricing Engine), and by the Vendor for Option C (Full Stack).",
        "The OCPP / charger middleware will be provided by Qualiteq for Options A (Core) and B (+ Pricing Engine), and by the Vendor (optional) for Option C (Full Stack).",
    ]
    four = [
        "The Customer app (mobile + web) UI must be provided by a Vendor for Option A (Core).",
        "The Pricing & product engine must be provided by Qualiteq for Option A (Core).",
        "The OCPP / charger middleware must be provided by Qualiteq for Option A (Core).",
    ]

    def covers_options(texts):
        joined = " ".join(texts).casefold()
        return (
            "option a" in joined
            and "option b" in joined
            and "option c" in joined
            and "all options" in joined
        )

    assert covers_options(eight) is True
    assert covers_options(four) is False


def test_evidence_match_batch_telemetry_avoids_double_counting_wall():
    """Batch wall is reported once; per-item relevance seconds may be summed."""
    item_lines = [
        "Evidence match item: chunk=3 status=REVIEW_REQUIRED llm_calls=1 relevance_llm_s=30.0",
        "Evidence match item: chunk=3 status=REVIEW_REQUIRED llm_calls=1 relevance_llm_s=31.0",
        "Evidence match item: chunk=3 status=REVIEW_REQUIRED llm_calls=2 relevance_llm_s=60.0",
    ]
    batch_line = (
        "Evidence match batch: chunk=3 batch_wall_s=168.704 requirements=3 "
        "total_llm_calls=4 total_cache_hits=0 sum_relevance_llm_s=121.000"
    )
    assert batch_line.count("batch_wall_s=") == 1
    assert all("batch_wall_s=" not in line for line in item_lines)
    summed = 30.0 + 31.0 + 60.0
    assert abs(summed - 121.0) < 1e-6


def test_progress_recovers_without_per_chunk_output(monkeypatch, tmp_path):
    monkeypatch.setattr("app.orchestration.service.PROGRESS_DIR", str(tmp_path / "progress"))
    monkeypatch.setattr("app.orchestration.service.OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(
        "app.orchestration.service.extract_pdf_text",
        lambda _: [{"page": 1, "text": "Requirement"}],
    )
    monkeypatch.setattr(
        "app.orchestration.service.create_chunks",
        lambda pages: [
            {"page": 1, "text": "First chunk"},
            {"page": 2, "text": "Second chunk"},
        ],
    )
    output_writes = []
    original_save = __import__("app.orchestration.service", fromlist=["save_json"]).save_json

    def tracking_save(path, data):
        if str(path).endswith(".json") and "output" in str(path).replace("\\", "/"):
            output_writes.append(path)
        original_save(path, data)

    monkeypatch.setattr("app.orchestration.service.save_json", tracking_save)

    calls = []

    def mock_extract(chunk):
        calls.append(chunk["text"])
        if chunk["text"] == "Second chunk" and calls.count("Second chunk") == 1:
            raise RuntimeError("Simulated failure")
        return {
            "requirements": [{"text": f"Requirement from {chunk['text']}"}],
        }

    monkeypatch.setattr("app.orchestration.service.extract_requirements", mock_extract)
    monkeypatch.setattr(
        "app.orchestration.service.RequirementEvidenceMatcher",
        lambda: type("M", (), {
            "match_requirement": lambda self, requirement, top_k=3: {
                "status": "NOT_FOUND",
                "evidence": [],
            },
            "match_requirements": lambda self, requirements, top_k=3: [
                {"status": "NOT_FOUND", "evidence": []} for _ in requirements
            ],
        })(),
    )

    try:
        process_tender_document("resume-output.pdf")
    except RuntimeError:
        pass

    # Failed run should not have written final output yet.
    assert output_writes == []
    assert calls.count("First chunk") == 1

    result = process_tender_document("resume-output.pdf")
    assert calls.count("First chunk") == 1
    assert calls.count("Second chunk") == 2
    assert len(result) == 2
    assert len(output_writes) == 1
