import json

import numpy as np
import pytest

import app.rag.retriever as rag_module
from app.rag.retriever import TenderRetriever, load_source_catalog
from app.response.response_builder import format_evidence_match


PTA_BID = "Bid Docs- Unified Cross_Platform 16-03-2026 (1) copy.pdf"
PESSI = "PESSI RSD FINAL (1) copy.docx"
KARANDAAZ = "RFP Response - Karandaaz final version copy.pdf"
EV_CHARGING = "RFP Response for EV Charging Network Apps copy.pdf"
UNKNOWN = "unlisted-cbs-proposal.pdf"

CATALOG_CHUNKS = [
    {"text": "PTA buyer tender bid docs for a unified platform.", "source": PTA_BID, "page": 1},
    {"text": "Karandaaz CBS proposal for digital finance services.", "source": KARANDAAZ, "page": 2},
    {"text": "EV Charging Network Apps CBS proposal for charger software.", "source": EV_CHARGING, "page": 3},
    {"text": "PESSI Punjab Employees Social Security RSD requirements.", "source": PESSI, "page": 4},
    {"text": "Unknown CBS proposal for an unlisted customer portal.", "source": UNKNOWN, "page": 5},
]


def _fake_embeddings(texts):
    vectors = []
    for text in texts:
        lowered = text.lower()
        if "pta" in lowered or "bid docs" in lowered or "buyer tender" in lowered:
            vectors.append([1.0, 0.0])
        elif "karandaaz" in lowered:
            vectors.append([0.97, 0.03])
        elif "ev charging" in lowered:
            vectors.append([0.94, 0.06])
        elif "unlisted" in lowered or "unknown" in lowered:
            vectors.append([0.90, 0.10])
        elif "pessi" in lowered:
            vectors.append([0.0, 1.0])
        else:
            # Queries about CBS proposals sit near the PTA/Karandaaz/EV cluster.
            vectors.append([1.0, 0.0])
    return np.asarray(vectors, dtype="float32")


def _write_catalog(path, catalog):
    path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")


PRODUCTION_LIKE_CATALOG = {
    PTA_BID: {"doc_kind": "buyer_tender", "allow_as_cbs_evidence": False},
    PESSI: {"doc_kind": "requirements_spec", "allow_as_cbs_evidence": True},
    EV_CHARGING: {"doc_kind": "cbs_proposal", "allow_as_cbs_evidence": True},
    KARANDAAZ: {"doc_kind": "cbs_proposal", "allow_as_cbs_evidence": True},
}


@pytest.fixture
def kb(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_module, "KB_DIR", tmp_path)
    monkeypatch.setattr(rag_module, "INDEX_PATH", tmp_path / "cbs.index")
    monkeypatch.setattr(rag_module, "METADATA_PATH", tmp_path / "chunks.json")
    monkeypatch.setattr(rag_module, "create_embeddings", _fake_embeddings)
    monkeypatch.delenv(rag_module.KB_EXCLUDE_SOURCES_ENV, raising=False)
    builder = TenderRetriever(persist=False, source_catalog={})
    builder.ingest([dict(chunk) for chunk in CATALOG_CHUNKS])
    builder.persist = True
    builder.save()
    return tmp_path


def _retriever(catalog=None):
    return TenderRetriever(source_catalog=catalog)


def test_unlisted_source_remains_searchable(kb):
    retriever = _retriever(PRODUCTION_LIKE_CATALOG)
    results = retriever.retrieve("CBS proposal", top_k=4)
    sources = [item["source"] for item in results]
    assert UNKNOWN in sources
    assert PTA_BID not in sources


def test_buyer_tender_never_appears_in_results(kb):
    retriever = _retriever(PRODUCTION_LIKE_CATALOG)
    results = retriever.retrieve("CBS proposal", top_k=5)
    assert [item["source"] for item in results]
    assert PTA_BID not in [item["source"] for item in results]
    assert retriever.active_catalog_blocks() == [PTA_BID]


def test_pessi_can_be_excluded_through_catalog(kb):
    catalog = {
        **PRODUCTION_LIKE_CATALOG,
        PESSI: {"doc_kind": "requirements_spec", "allow_as_cbs_evidence": False},
    }
    allowed = _retriever(PRODUCTION_LIKE_CATALOG).retrieve("PESSI social security", top_k=1)
    assert allowed[0]["source"] == PESSI

    blocked = _retriever(catalog).retrieve("PESSI social security", top_k=3)
    assert PESSI not in [item["source"] for item in blocked]
    assert blocked


def test_karandaaz_and_ev_charging_remain_searchable(kb):
    retriever = _retriever(PRODUCTION_LIKE_CATALOG)
    results = retriever.retrieve("CBS proposal", top_k=4)
    sources = [item["source"] for item in results]
    assert KARANDAAZ in sources
    assert EV_CHARGING in sources


def test_env_source_exclusion_still_works_with_catalog(kb, monkeypatch):
    monkeypatch.setenv(rag_module.KB_EXCLUDE_SOURCES_ENV, KARANDAAZ)
    retriever = TenderRetriever(source_catalog=PRODUCTION_LIKE_CATALOG)
    results = retriever.retrieve("CBS proposal", top_k=3)
    sources = [item["source"] for item in results]
    assert KARANDAAZ not in sources
    assert PTA_BID not in sources
    assert EV_CHARGING in sources
    assert retriever.active_exclusions() == [KARANDAAZ]


def test_filtering_does_not_change_index_size_or_chunk_order(kb):
    retriever = _retriever(PRODUCTION_LIKE_CATALOG)
    ntotal = retriever.index.ntotal
    dimension = retriever.index.d
    sources = [chunk["source"] for chunk in retriever.chunks]
    texts = [chunk["text"] for chunk in retriever.chunks]
    first_vector = retriever.index.reconstruct(0).copy()

    retriever.retrieve("CBS proposal", top_k=3)

    assert retriever.index.ntotal == ntotal == len(CATALOG_CHUNKS)
    assert retriever.index.d == dimension
    assert [chunk["source"] for chunk in retriever.chunks] == sources
    assert [chunk["text"] for chunk in retriever.chunks] == texts
    assert np.allclose(retriever.index.reconstruct(0), first_vector)
    stored = json.loads((kb / "chunks.json").read_text(encoding="utf-8"))
    assert [chunk["source"] for chunk in stored] == sources


def test_top_k_is_filled_from_allowed_candidates(kb):
    retriever = _retriever(PRODUCTION_LIKE_CATALOG)
    results = retriever.retrieve("CBS proposal", top_k=2)
    assert len(results) == 2
    assert [item["source"] for item in results] == [KARANDAAZ, EV_CHARGING]
    scores = [item["score"] for item in results]
    assert scores == sorted(scores, reverse=True)


def test_missing_catalog_does_not_crash_and_keeps_all_sources(kb, capsys):
    retriever = TenderRetriever()
    results = retriever.retrieve("CBS proposal", top_k=5)
    sources = [item["source"] for item in results]
    assert PTA_BID in sources
    assert KARANDAAZ in sources
    assert "KB_CATALOG missing" in capsys.readouterr().out


def test_malformed_catalog_does_not_crash_retrieval(kb, capsys):
    (kb / rag_module.CATALOG_FILENAME).write_text("{not json", encoding="utf-8")
    retriever = TenderRetriever()
    results = retriever.retrieve("CBS proposal", top_k=3)
    assert results
    assert PTA_BID in [item["source"] for item in results]
    assert "KB_CATALOG unreadable" in capsys.readouterr().out


def test_invalid_catalog_object_fail_open(kb, capsys):
    (kb / rag_module.CATALOG_FILENAME).write_text("[]", encoding="utf-8")
    catalog = load_source_catalog(kb / rag_module.CATALOG_FILENAME)
    assert catalog == {}
    assert "not_an_object" in capsys.readouterr().out


def test_published_evidence_keys_unchanged_when_catalog_filters(kb):
    retriever = _retriever(PRODUCTION_LIKE_CATALOG)
    results = retriever.retrieve("CBS proposal", top_k=1)
    formatted = format_evidence_match({
        "status": "REVIEW_REQUIRED",
        "note": "verify",
        "evidence": [{
            "text": results[0]["text"],
            "source": results[0]["source"],
            "page": results[0]["page"],
            "similarity": results[0]["score"],
        }],
    })
    assert set(formatted["evidence"][0]) == {"text", "source", "page", "similarity"}
    assert formatted["evidence"][0]["source"] == KARANDAAZ
