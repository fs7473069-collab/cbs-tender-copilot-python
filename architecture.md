# Python Document Intelligence Architecture

## 1. Purpose

The Python Document Intelligence service is responsible for processing tender documents and converting their content into structured information for the RFP workflow.

The current MVP supports:

* PDF and DOCX document extraction
* Table extraction
* Text chunking
* LLM-based requirement extraction
* Structured JSON output
* Basic validation
* Source page tracking

The architecture is designed to be reusable for different RFP and document-processing use cases.

---

## 2. High-Level Architecture

The current MVP integration uses S3 for document storage and RabbitMQ for communication between the Java backend and Python service.

```text
                    Java Backend
                         |
              Upload tender document
                         |
                         v
                  S3 / Object Storage
                         |
                         | Document reference
                         v
                    RabbitMQ
                         |
                         | RFP analysis message
                         v
              Python Document Service
                         |
              +----------+----------+
              |                     |
              v                     v
      Document Processing       LLM Processing
              |                     |
       PDF / DOCX / XLSX           |
              |                     |
              v                     |
        Text & Tables              |
              |                     |
              v                     |
           Chunking                |
              |                     |
              +---------> Qwen3 8B
                              |
                              v
                    Structured Requirements
                              |
                              v
                         Validation
                              |
                              v
                         RabbitMQ
                              |
                              v
                       Java Backend
```

---

## 3. Python Responsibilities

The Python service is responsible for:

* Receiving RFP processing requests through RabbitMQ
* Reading document references from incoming messages
* Downloading documents from S3/object storage
* Extracting text from supported documents
* Extracting tables where supported
* Preserving source/page information where available
* Creating manageable text chunks
* Sending chunks to the LLM
* Extracting structured requirements
* Validating the LLM response
* Returning structured results through RabbitMQ

The Python service does not own:

* Organization management
* User management
* Authentication
* Main application/business ownership

These responsibilities remain with the Java backend.

---

## 4. Current Project Structure

```text
RFP project/
|
|-- app/
|   |
|   |-- document/
|   |   |-- __init__.py
|   |   |-- pdf_processor.py
|   |   |-- docx_processor.py
|   |   |-- table_extractor.py
|   |   -- chunker.py
|   |
|   |-- requirements/
|   |   |-- __init__.py
|   |   -- extractor.py
|   |
|   |-- llm/
|   |   |-- __init__.py
|   |   |-- ollama_client.py
|   |   -- ollama_4b_client.py
|   |
|   |-- messaging/
|   |   |-- __init__.py
|   |   |-- rabbitmq.py
|   |   |-- consumer.py
|   |   -- publisher.py
|   |
|   -- storage/
|       |-- __init__.py
|       -- s3_client.py
|
|-- data/
|   |-- s3_test/
|   |   -- tender.docx
|   -- sample tender.pdf
|
|-- chunks/
|   |-- ev_tender_chunks.json
|   |-- cbs_tender_chunks.json
|   -- requirements/
|       |-- ev_tender_requirements.json
|       -- cbs_tender_requirements.json
|
|-- tests/
|   |-- __init__.py
|   -- test_document_processing.py
|
|-- architecture.md
|-- evaluation.md
|-- quality_evaluation.py
|-- pipeline.py
|-- main.py
|-- benchmark_4b.py
|-- llm_results.json
-- tables.json
```

Generated cache directories such as `__pycache__` and `.pytest_cache` are not part of the application architecture.

---

## 5. Document Processing

### PDF Processor

File:

`app/document/pdf_processor.py`

Responsibility:

* Open PDF documents
* Process pages individually
* Extract text from each page
* Preserve page numbers

Output:

```json
{
  "page": 1,
  "text": "Extracted page text..."
}
```

---

### DOCX Processor

File:

`app/document/docx_processor.py`

Responsibility:

* Open DOCX documents
* Extract non-empty paragraphs
* Combine document text for processing

Current MVP behavior treats the DOCX document as a single logical page.

Future versions can improve DOCX source tracking to identify actual document pages or sections.

---

### Table Extractor

File:

`app/document/table_extractor.py`

Responsibility:

* Detect tables in supported documents
* Extract table rows
* Preserve source page information where available

Example:

```text
Table
 |-- Page
 -- Rows
```

---

## 6. Chunking

File:

`app/document/chunker.py`

The chunker divides extracted document text into smaller units suitable for LLM processing.

Current configuration:

```text
Maximum chunk size: 800 characters
```

Each chunk preserves its source page where available.

Example:

```json
{
  "page": 4,
  "text": "Customer app must support OTP login..."
}
```

The purpose of chunking is to avoid sending an entire tender document to the LLM at once and to create manageable processing units.

---

## 7. LLM Processing

The MVP currently uses:

**Qwen3 8B running locally through Ollama.**

The LLM is responsible for extracting explicit requirements from each document chunk.

Current flow:

```text
Document
    |
    v
Text Extraction
    |
    v
Chunking
    |
    v
Qwen3 8B
    |
    v
Structured JSON
    |
    v
Python Validation
```

The LLM is instructed to:

* Extract only explicit requirements
* Avoid inventing information
* Classify requirements
* Identify mandatory requirements
* Provide confidence
* Return valid JSON only

Supported categories:

* Functional
* Technical
* Security
* Commercial
* Non-Functional
* Unclear

Example output:

```json
{
  "requirements": [
    {
      "text": "Customer app must support OTP login",
      "category": "Functional",
      "mandatory": true,
      "confidence": "High"
    }
  ]
}
```

Page information is maintained by the Python processing layer rather than being generated by the LLM.

---

## 8. LLM Model Research

Two local models were tested during the initial research:

* Qwen3 4B
* Qwen3 8B

The initial tests showed that Qwen3 8B produced cleaner structured JSON and was more reliable for the current requirement-extraction task.

Therefore:

```text
MVP Model = Qwen3 8B
Runtime   = Ollama
```

Qwen3 4B remains part of the research/benchmarking work and may be reconsidered later depending on performance and resource requirements.

---

## 9. Requirement Extraction

The requirement extraction layer converts LLM output into structured requirements.

Each requirement contains:

```text
Requirement
|-- text
|-- category
|-- mandatory
-- confidence
```

The processing layer associates the requirement with its source page where available.

Example:

```text
Requirement:
Customer app must support OTP login

Category:
Functional

Mandatory:
true

Confidence:
High

Source:
Page 4
```

---

## 10. Source Tracking

Source information is preserved throughout the document-processing pipeline.

```text
Document
   |
   +--> Page
          |
          +--> Chunk
                 |
                 +--> Requirement
```

For PDF documents, page numbers are preserved directly during extraction.

For the current DOCX MVP, the document is treated as one logical page. More precise DOCX section/page tracking can be added later.

Future source tracking may include:

* Document ID
* Document filename
* Page number
* Section
* Heading
* Chunk ID
* Table reference

---

## 11. S3 / Object Storage

The Java backend stores tender documents in S3/object storage.

Python receives the storage reference rather than the complete document through RabbitMQ.

Example:

```json
{
  "documentId": "12345",
  "filename": "tender.docx",
  "bucket": "rfp-copilot-dev-...",
  "key": "organizations/.../documents/..."
}
```

Python then downloads the document from S3 for processing.

This keeps large document files out of RabbitMQ messages.

---

## 12. RabbitMQ Integration

RabbitMQ is used as the messaging layer between Java and Python.

The current integration pattern is:

```text
Java
  |
  | RFP analysis request
  v
RabbitMQ
  |
  v
Python Consumer
  |
  v
Download document from S3
  |
  v
Process document
  |
  v
Run LLM
  |
  v
Generate structured result
  |
  v
RabbitMQ
  |
  v
Java
```

The production RFP queue is:

```text
copilot-analysis
```

For isolated Python testing, a separate test queue is used:

```text
rfp-python-test
```

The test queue prevents development testing from interfering with the production/application analysis queue.

---

## 13. Current MVP End-to-End Flow

The current MVP processing flow is:

```text
Tender Document
      |
      v
PDF / DOCX Extraction
      |
      v
Text / Tables
      |
      v
Chunking
      |
      v
Qwen3 8B
      |
      v
Structured Requirements JSON
      |
      v
Python Validation
      |
      v
Result
```

The integration flow is:

```text
Java
  |
  +--> S3
  |      |
  |      +--> Tender Document
  |
  +--> RabbitMQ
         |
         +--> Python Consumer
                 |
                 +--> S3 Download
                 |
                 +--> Document Processing
                 |
                 +--> Qwen3 8B
                 |
                 +--> Validation
                 |
                 +--> RabbitMQ Result
                          |
                          v
                         Java
```

---

## 14. Error Handling

The service should handle common processing problems such as:

* Unsupported file types
* Corrupted documents
* Empty documents
* Pages with no extractable text
* Tables that cannot be extracted
* Invalid LLM JSON
* Missing required fields
* S3 download failures
* RabbitMQ connection failures
* Scanned/image-only documents

Scanned documents may require OCR because normal text extraction cannot extract text from image-only pages.

---

## 15. Testing and Validation

The document-processing components have been tested using sample tender documents.

### EV Charging Network Tender

Initial document-processing validation:

* PDF pages extracted: 11
* Tables detected: 14
* Chunks created: 34
* LLM chunks processed: 34
* Valid JSON responses: 34/34
* Requirements extracted: 143

### CBS Tender Copilot DOCX

Current MVP validation:

* DOCX downloaded successfully from S3
* RabbitMQ message received successfully
* Document downloaded successfully
* DOCX text extracted successfully
* Characters extracted: 16,591
* Chunks created: 21
* Requirements extracted: 75

These tests confirm that the main document-processing and local LLM pipeline is functioning.

---

## 16. Current Status

The following components are currently working:

* PDF extraction
* DOCX extraction
* Table extraction
* Text chunking
* Qwen3 8B integration
* Structured requirement extraction
* Basic LLM response validation
* S3 document download
* RabbitMQ connection
* RabbitMQ message consumption
* End-to-end document retrieval through S3 + RabbitMQ

The current focus is the RFP MVP.

---

## 17. Future Extensions

Future versions can extend the architecture with:

* XLSX document processing
* OCR for scanned documents
* Improved DOCX source/page tracking
* More detailed source references
* Requirement deduplication
* Requirement normalization
* RAG / vector search
* Compliance matrix generation
* Knowledge retrieval
* Agentic RFP workflows
* Human-in-the-loop review
* Additional LLM providers
* REST API integration where required
* More comprehensive evaluation datasets

---

## 18. Architecture Goal

The architecture keeps the major responsibilities separated:

```text
Storage
   |
   +--> S3

Messaging
   |
   +--> RabbitMQ

Document Intelligence
   |
   +--> PDF
   +--> DOCX
   +--> XLSX
   +--> Tables
   +--> Chunking

AI
   |
   +--> Qwen3 8B
   +--> Requirement Extraction

Validation
   |
   +--> Structured Output Checks

Future AI
   |
   +--> RAG
   +--> Agentic Workflows
```

This separation allows individual components to be improved or replaced without changing the complete RFP system.

The overall design follows the RFP AI principle:

```text
Understand
    v
Structure
    v
Retrieve
    v
Recommend
    v
Act
    v
Review
```

The current MVP primarily implements the **Understand -> Structure** stages.
