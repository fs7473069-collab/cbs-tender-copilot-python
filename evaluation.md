# Document Processing and LLM Evaluation

## 1. Purpose

This document records the validation and evaluation of the Python RFP Document Intelligence pipeline.

The evaluation covers:

* Document extraction
* Table extraction
* Text chunking
* Source tracking
* LLM requirement extraction
* Structured JSON validation
* Qwen3 4B vs Qwen3 8B comparison
* S3 document retrieval
* RabbitMQ message consumption

---

# 2. Test Document

## EV Charging Network Tender

**Document:** EV Charging Network: Customer App & Operator Console

* SOW Phase 1 Pilot
* 6-Station Network
* Dar es Salaam, Tanzania
* 11 pages

This document was used for the initial document-processing and LLM evaluation.

---

# 3. PDF Text Extraction

The PDF processor was tested against the 11-page EV Charging Network tender.

Results:

* Pages extracted: 11
* Page numbers preserved: Yes
* Readable text: Yes
* Empty pages found: No

**Result: PASS**

---

# 4. Table Extraction

The table extraction component was tested against the same document.

Results:

* Tables detected: 14
* Table rows extracted: Yes
* Headers/columns generally preserved: Yes
* Some empty rows/cells: Yes

**Result: PASS with minor formatting artifacts**

The artifacts appear to be related to PDF table layout and extraction behavior.

---

# 5. Chunking

The document was processed by the chunking component.

Results:

* Total chunks: 34
* Source page preserved: Yes
* Important tender content retained: Yes

The chunker creates manageable text units for downstream LLM processing.

**Result: PASS**

---

# 6. Important Content Check

The extracted chunks were checked for important tender terminology and requirements.

Examples successfully found:

* OTP
* OCPP
* M-Pesa
* Tigo Pesa
* Airtel Money
* Must-have requirements
* Payment requirements
* Security requirements
* Mobile application requirements

**Result: PASS**

---

# 7. Source Tracking

Source tracking was validated through the processing pipeline.

The following relationship is maintained:

```text
Document
   ↓
Page
   ↓
Chunk
   ↓
Requirement
```

For PDF documents:

* Page numbers are preserved during extraction.
* Chunks retain their source page.
* Requirements can be associated with the source page.

**Result: PASS**

---

# 8. UTF-8 / Special Character Handling

Extracted text and JSON data were tested using UTF-8 encoding.

Special characters were preserved correctly when files were read using UTF-8.

Some corrupted-looking characters observed during PowerShell display were related to terminal encoding rather than corruption of the stored JSON data.

**Result: PASS**

---

# 9. LLM Requirement Extraction

The requirement extraction pipeline was tested using local Ollama models.

Models evaluated:

* Qwen3 4B
* Qwen3 8B

The LLM was instructed to:

* Extract explicit requirements only
* Avoid inventing requirements
* Classify requirements
* Identify mandatory requirements
* Return structured JSON
* Provide confidence
* Avoid extracting headings or general document information

---

# 10. Qwen3 8B Evaluation

Qwen3 8B was tested against all 34 EV tender chunks.

Results:

* Chunks processed: 34
* Valid JSON responses: 34
* Invalid JSON responses: 0
* Requirements extracted: 143

Validation checks performed:

* Invalid categories: 0
* Wrong source pages: 0
* Empty requirement text: 0
* Invalid mandatory values: 0
* Invalid confidence values: 0

Additional quality review identified a small number of possible:

* Fragmentary requirements
* Non-requirement/headline extractions
* Category drift

These were reviewed as quality issues rather than JSON/schema failures.

**Result: PASS**

---

# 11. Qwen3 4B Evaluation

Qwen3 4B was tested on representative tender chunks to compare its performance with Qwen3 8B.

During the initial benchmark, some responses required significant processing time and did not reliably return valid JSON on the first attempt.

Example observations:

| Chunk | Page | Processing Time | JSON Result  |
| ----- | ---: | --------------: | ------------ |
| 3     |    2 |         ~86 sec | Invalid JSON |
| 10    |    4 |         ~75 sec | Invalid JSON |

The benchmark was stopped rather than running a full 34-chunk evaluation, because of slow processing and unreliable JSON output.

Qwen3 4B was therefore assessed only on representative chunks. This evaluation does not claim a complete 34-chunk Qwen3 4B benchmark.

**Result: NOT SELECTED for the current MVP**

---

# 12. Model Decision

Qwen3 8B is the current MVP model for requirement extraction.

This decision is based on the verified EV tender run:

* All 34 chunks produced valid JSON.
* 143 requirements were extracted.
* Schema checks for category, mandatory, confidence, empty text, and source page passed on that run.

Qwen3 4B remains a research candidate. It was slower on the sampled chunks and did not reliably return valid JSON, so the 4B benchmark was stopped early.

This is an MVP selection for the current local Ollama setup and the tested EV tender. It does not claim that Qwen3 8B is universally better than Qwen3 4B, or that 8B has 100% semantic accuracy.

Possible fragments, headings, and similar extractions remain quality warnings rather than schema failures.

---

# 13. CBS DOCX / Integration Evaluation

The CBS tender (`tender.docx`) was used to validate document retrieval and DOCX processing in the S3 + RabbitMQ integration path.

Verified integration steps:

* RabbitMQ message received successfully using the isolated test queue `rfp-python-test`.
* S3 document download succeeded.
* DOCX file processed: `tender.docx`.
* Extracted characters: 16,591.
* Chunks created: 21.

Existing CBS requirements result:

* 75 requirements.

That 75-requirement file is from an earlier pipeline run. It does not contain the newer `source` metadata (`page` and 1-based `chunk`).

The updated pipeline code attaches that source metadata on fresh runs. A later full fresh CBS run did not complete: Qwen3 8B timed out at chunk 9/21 after 120 seconds.

This evaluation does not claim that a full 21-chunk fresh CBS pipeline run completed successfully.

**Result: PASS for S3 download, isolated-queue consumption, DOCX extraction, and chunking. Fresh full LLM run: INCOMPLETE (timeout).**

---

# 14. RabbitMQ Evaluation

Two queues are defined in the project:

* Production/application queue: `copilot-analysis`
* Isolated Python test queue: `rfp-python-test`

Verified:

* The isolated `rfp-python-test` queue was successfully tested. A message was received and processing started from that queue.

Not claimed:

* The production `copilot-analysis` queue was not consumed as part of this evaluation.
* A full production end-to-end Java-to-Python-to-Java test on `copilot-analysis` is not claimed.

**Result: PASS for isolated test-queue consumption only**

---

# 15. S3 Evaluation

S3 download was successfully tested as part of the CBS DOCX integration path.

Python received the storage reference, downloaded `tender.docx`, and then extracted text locally.

**Result: PASS**

---

# 16. Automated Tests

`pytest` was run against the project test suite.

Result:

* 6 passed in 1.60s

`test_all_chunks.py` was deleted because it was an outdated experimental script. It is not part of the current evaluation or project structure.

**Result: PASS**

---

# 17. Overall Conclusion

The Python document-intelligence MVP has been validated on the EV Charging Network PDF for extraction, tables, chunking, source-page association, and Qwen3 8B structured JSON output (34/34 valid JSON, 143 requirements). Schema checks on that 8B run passed; remaining issues are quality warnings, not schema failures.

Qwen3 8B is the current MVP model. Qwen3 4B was sampled, then stopped due to slow runtime and invalid JSON, and remains a research candidate.

CBS integration was validated through S3 download, isolated RabbitMQ test-queue consumption, DOCX extraction (16,591 characters), and 21 chunks. An earlier CBS pipeline produced 75 requirements without the new source metadata. A later fresh CBS LLM run timed out at chunk 9/21 after 120 seconds and did not finish.

---

# 18. Current Limitations

* Qwen3 4B was not benchmarked on all 34 EV chunks.
* Qwen3 8B JSON/schema success on the EV tender is not the same as 100% semantic accuracy.
* Possible fragments and headings are still quality warnings.
* The existing CBS 75-requirement file does not include the new source metadata.
* A full fresh CBS 21-chunk Qwen3 8B run did not complete (timeout at chunk 9/21 after 120 seconds).
* The production RabbitMQ queue `copilot-analysis` was not consumed or fully end-to-end tested in this evaluation.
* Isolated-queue success does not prove production-queue behaviour.
* OCR for scanned/image-only documents is not part of this evaluation.
