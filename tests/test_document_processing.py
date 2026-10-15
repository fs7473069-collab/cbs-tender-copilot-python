from app.document.pdf_processor import extract_pdf_text
from app.document.chunker import create_chunks
from app.document.table_extractor import extract_pdf_tables
from app.requirements.extractor import Requirement, prepare_llm_input

PDF_PATH = "data/sample tender.pdf"


def test_pdf_text_extraction():
    pages = extract_pdf_text(PDF_PATH)
    assert len(pages) == 11
    assert pages[0]["page"] == 1
    assert pages[-1]["page"] == 11
    assert pages[0]["text"]


def test_chunk_creation():
    pages = extract_pdf_text(PDF_PATH)
    chunks = create_chunks(pages)
    assert len(chunks) == 34
    assert chunks[0]["page"] == 1
    assert chunks[0]["text"]


def test_table_extraction():
    tables = extract_pdf_tables(PDF_PATH)
    assert len(tables) == 14

    for table in tables:
        assert "page" in table
        assert "rows" in table
        assert table["page"] >= 1


def test_requirement_structure():
    requirement = Requirement(
        text="Customer app must support OTP login",
        category="Functional",
        mandatory=True,
        page=4,
        confidence="High"
    )

    assert requirement.text == "Customer app must support OTP login"
    assert requirement.category == "Functional"
    assert requirement.mandatory is True
    assert requirement.page == 4
    assert requirement.confidence == "High"


def test_source_tracking():
    pages = extract_pdf_text(PDF_PATH)

    requirement = Requirement(
        text="Customer app must support OTP login",
        category="Functional",
        mandatory=True,
        page=4,
        confidence="High"
    )

    source_page = next(
        page for page in pages
        if page["page"] == requirement.page
    )

    assert source_page["page"] == 4
    assert source_page["text"]


def test_llm_input_preparation():
    pages = extract_pdf_text(PDF_PATH)
    chunks = create_chunks(pages)

    llm_input = prepare_llm_input(chunks[0])

    assert "page" in llm_input
    assert "text" in llm_input
    assert llm_input["page"] == 1
    assert llm_input["text"]