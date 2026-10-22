
import pytest

from app.orchestration.service import process_tender_document


def test_process_pdf_document(monkeypatch):
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


def test_rejects_invalid_llm_json(monkeypatch):
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


def test_rejects_invalid_requirements_list(monkeypatch):
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
