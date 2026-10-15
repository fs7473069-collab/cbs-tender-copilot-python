
from docx import Document

doc = Document("data/s3_test/tender.docx")

page_breaks = 0

for paragraph in doc.paragraphs:
    for run in paragraph.runs:
        page_breaks += run._element.xml.count('w:type="page"')
        page_breaks += run._element.xml.count("w:br")

print("Paragraphs:", len(doc.paragraphs))
print("Sections:", len(doc.sections))
print("Explicit page-break elements:", page_breaks)
