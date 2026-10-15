from app.document.pdf_processor import extract_pdf_text
from app.document.chunker import create_chunks
from app.document.table_extractor import extract_pdf_tables
from app.requirements.extractor import Requirement, prepare_llm_input

PDF_PATH = "data/sample tender.pdf"


def main():
    pages = extract_pdf_text(PDF_PATH)
    chunks = create_chunks(pages)
    tables = extract_pdf_tables(PDF_PATH)

    print("Pages extracted:", len(pages))
    print("Chunks created:", len(chunks))
    print("Tables extracted:", len(tables))

    print()
    print("Requirement test:")

    requirement = Requirement(
        text="Customer app must support OTP login",
        category="Functional",
        mandatory=True,
        page=4,
        confidence="High"
    )

    print(requirement)

    print()
    print("Source tracking test:")
    print("Requirement source page:", requirement.page)

    source_page = next(
        page for page in pages
        if page["page"] == requirement.page
    )

    print()
    print("Requirement source text:")
    print(source_page["text"][:300])

    print()
    print("LLM input test:")

    llm_input = prepare_llm_input(chunks[0])
    print(llm_input)

    print()
    print("Chunk source tracking test:")
    print("Chunk source page:", chunks[0]["page"])


if __name__ == "__main__":
    main()