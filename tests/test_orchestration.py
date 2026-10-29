
import pytest

from app.orchestration.service import process_tender_document


def isolate_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.orchestration.service.PROGRESS_DIR",
        str(tmp_path / "progress"),
    )
    monkeypatch.setattr(
        "app.orchestration.service.OUTPUT_DIR",
        str(tmp_path / "output"),
    )


@pytest.fixture(autouse=True)
def mock_evidence_matcher(monkeypatch):
    """Ordinary orchestration tests must not call a local Ollama server."""
    class Matcher:
        def match_requirement(self, requirement, top_k=3):
            return {"status": "NOT_FOUND", "evidence": [], "note": "Mocked"}

    monkeypatch.setattr(
        "app.orchestration.service.RequirementEvidenceMatcher", Matcher,
    )


def test_process_pdf_document(monkeypatch, tmp_path):
    isolate_storage(monkeypatch, tmp_path)

    monkeypatch.setattr(
        "app.orchestration.service.extract_pdf_text",
        lambda path: [{"page": 2, "text": "Tender requirement text"}],
    )
    monkeypatch.setattr(
        "app.orchestration.service.create_chunks",
        lambda pages: [{"page": 2, "text": "Tender requirement text"}],
    )
    monkeypatch.setattr(
        "app.orchestration.service.extract_requirements",
        lambda chunk: (
            '{"requirements": [{"text": "System must support login", '
            '"category": "Functional", "mandatory": true, '
            '"confidence": "High"}]}'
        ),
    )

    result = process_tender_document("tender.pdf")

    assert len(result) == 1
    assert result[0]["text"] == "System must support login"
    assert result[0]["source"]["page"] == 2
    assert result[0]["source"]["chunk"] == 1


def test_rejects_unsupported_file_type():
    with pytest.raises(ValueError, match="Unsupported file type"):
        process_tender_document("tender.xlsx")


def test_rejects_empty_document(monkeypatch):
    monkeypatch.setattr(
        "app.orchestration.service.extract_pdf_text",
        lambda path: [{"page": 1, "text": "   "}],
    )

    with pytest.raises(ValueError, match="no extractable text"):
        process_tender_document("empty.pdf")


def test_rejects_invalid_llm_json(monkeypatch, tmp_path):
    isolate_storage(monkeypatch, tmp_path)

    monkeypatch.setattr(
        "app.orchestration.service.extract_pdf_text",
        lambda path: [{"page": 1, "text": "Requirement"}],
    )
    monkeypatch.setattr(
        "app.orchestration.service.create_chunks",
        lambda pages: [{"page": 1, "text": "Requirement"}],
    )
    monkeypatch.setattr(
        "app.orchestration.service.extract_requirements",
        lambda chunk: "not valid JSON",
    )

    with pytest.raises(ValueError, match="Invalid JSON"):
        process_tender_document("tender.pdf")


def test_rejects_invalid_requirements_list(monkeypatch, tmp_path):
    isolate_storage(monkeypatch, tmp_path)

    monkeypatch.setattr(
        "app.orchestration.service.extract_pdf_text",
        lambda path: [{"page": 1, "text": "Requirement"}],
    )
    monkeypatch.setattr(
        "app.orchestration.service.create_chunks",
        lambda pages: [{"page": 1, "text": "Requirement"}],
    )
    monkeypatch.setattr(
        "app.orchestration.service.extract_requirements",
        lambda chunk: '{"requirements": "not a list"}',
    )

    with pytest.raises(ValueError, match="requirements list"):
        process_tender_document("tender.pdf")

def test_resume_skips_completed_chunks(monkeypatch, tmp_path):
    isolate_storage(monkeypatch, tmp_path)

    monkeypatch.setattr(
        "app.orchestration.service.extract_pdf_text",
        lambda path: [{"page": 1, "text": "Requirement"}],
    )
    monkeypatch.setattr(
        "app.orchestration.service.create_chunks",
        lambda pages: [
            {"page": 1, "text": "First chunk"},
            {"page": 2, "text": "Second chunk"},
        ],
    )

    calls = []

    def mock_extract(chunk):
        calls.append(chunk["text"])
        if chunk["text"] == "Second chunk" and calls.count("Second chunk") == 1:
            raise RuntimeError("Simulated failure")

        return '{"requirements": [{"text": "Requirement from ' + chunk["text"] + '"}]}'

    monkeypatch.setattr(
        "app.orchestration.service.extract_requirements",
        mock_extract,
    )

    with pytest.raises(RuntimeError, match="Simulated failure"):
        process_tender_document("resume-test.pdf")

    result = process_tender_document("resume-test.pdf")

    assert calls.count("First chunk") == 1
    assert calls.count("Second chunk") == 2
    assert len(result) == 2


def test_process_attaches_evidence_match(monkeypatch, tmp_path):
    isolate_storage(monkeypatch, tmp_path)

    monkeypatch.setattr(
        "app.orchestration.service.extract_pdf_text",
        lambda path: [{"page": 1, "text": "Tender requirement"}],
    )
    monkeypatch.setattr(
        "app.orchestration.service.create_chunks",
        lambda pages: [{"page": 1, "text": "Tender requirement"}],
    )
    monkeypatch.setattr(
        "app.orchestration.service.extract_requirements",
        lambda chunk: {
            "requirements": [
                {
                    "text": "The system must support vehicle tracking.",
                    "category": "Functional",
                    "mandatory": True,
                    "confidence": "High",
                }
            ]
        },
    )

    class FakeMatcher:
        def match_requirement(self, requirement, top_k=3):
            return {
                "requirement": requirement["text"],
                "status": "REVIEW_REQUIRED",
                "evidence": [
                    {
                        "text": "Sample evidence for testing only.",
                        "source": "sample.docx",
                        "page": 2,
                        "similarity": 0.8,
                    }
                ],
                "note": "Test evidence; verify before use.",
            }

    monkeypatch.setattr(
        "app.orchestration.service.RequirementEvidenceMatcher",
        FakeMatcher,
    )

    result = process_tender_document("integration-test.pdf")

    assert len(result) == 1
    assert result[0]["evidence_match"]["status"] == "REVIEW_REQUIRED"
    assert result[0]["evidence_match"]["evidence"][0]["source"] == "sample.docx"
    assert result[0]["source"]["page"] == 1

    import json
    import app.orchestration.service as service

    output_files = list(
        (tmp_path / "output").glob("*.json")
    )
    assert len(output_files) == 1

    saved = json.loads(output_files[0].read_text(encoding="utf-8"))
    assert saved[0]["evidence_match"]["status"] == "REVIEW_REQUIRED"
