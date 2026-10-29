# RFP Document Intelligence

Python-based Document Intelligence MVP for the Reusable RFP AI Agent / CBS Tender Copilot.

The service processes tender documents, extracts structured requirements, and integrates with S3/object storage and RabbitMQ.

## Architecture

The current MVP follows this flow:

```text
Java Backend
    |
    +--> S3 / Object Storage
    |       |
    |       +--> Tender Document
    |
    +--> RabbitMQ
            |
            v
      Python Document Service
            |
            +--> PDF / DOCX extraction
            +--> Table extraction
            +--> Chunking
            +--> Qwen3 8B via Ollama
            +--> Requirement validation
            |
            v
         RabbitMQ
            |
            v
       Java Backend
```

The Java upload service stores the tender file in S3 and sends a document reference over the request queue. Python downloads the file from the S3 bucket/key in the message, extracts text, chunks it, runs Qwen3 8B locally through Ollama, and publishes an `RFP_ANALYSIS_COMPLETED` JSON response to `rfp_docs_response1` for the Java backend.

Python does not own users, organisations, or authentication. Those stay on the Java backend.

For the full design, see [architecture.md](architecture.md). For measured results and limitations, see [evaluation.md](evaluation.md).

## Current MVP

- PDF and DOCX text extraction
- PDF table extraction
- Rule-based chunking with page tracking
- Qwen3 8B requirement extraction through Ollama
- Structured JSON with category, mandatory, and confidence fields
- Source metadata on fresh pipeline runs (`page` and 1-based `chunk`)
- S3 document download
- RabbitMQ request consumption and response publishing
- Response builder for `RFP_ANALYSIS_COMPLETED` messages
- Automated document-processing, response-builder, and consumer tests

**MVP model: Qwen3 8B.** Qwen3 4B remains a research candidate.

Requirement categories:

- Functional
- Technical
- Security
- Commercial
- Non-Functional
- Unclear

## Project Layout

```text
app/
  document/       PDF, DOCX, tables, chunking
  requirements/   Requirement data structures and LLM input preparation
  llm/            Ollama clients (8B MVP, 4B research)
  response/       Completion response builder
  messaging/      RabbitMQ connection, consumer, publisher
  storage/        S3 download

data/              Sample PDF and S3 test downloads
chunks/            Saved chunks and requirement JSON

tests/
  test_document_processing.py
  test_response_builder.py
  test_consumer.py

pipeline.py        DOCX -> chunks -> Qwen3 8B -> JSON
main.py            Local PDF extraction smoke test
requirements.txt   Python dependencies
```

## Setup

### 1. Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install the required packages

```powershell
pip install -r requirements.txt
```

### 3. Install Ollama and pull the MVP model

Install [Ollama](https://ollama.com/) and run:

```powershell
ollama pull qwen3:8b
```

Ollama should be reachable at:

```text
http://localhost:11434
```

### 4. Configure environment variables

Create a local `.env` file with the required values.

**Do not commit secrets.**

```dotenv
S3_REGION=
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=

RABBITMQ_HOST=
RABBITMQ_PORT=5672
RABBITMQ_VHOST=/
RABBITMQ_USERNAME=
RABBITMQ_PASSWORD=

RABBITMQ_QUEUE=copilot-analysis
```

The request queue is configurable through `RABBITMQ_QUEUE`. The application currently uses `copilot-analysis` in its configured integration environment. The consumer's code-level fallback is `rfp-python-test` when the environment variable is not set.

The response queue is currently hard-coded in `app/messaging/publisher.py` as:

```text
rfp_docs_response1
```

Zeeshan has confirmed that the Java backend consumes `rfp_docs_response1`. Use a separate request queue (or isolated test queue) during development so local consumers do not take production analysis messages.

## Run

### Local PDF smoke test

```powershell
python main.py
```

### DOCX requirement pipeline

The pipeline writes:

```text
chunks/cbs_tender_chunks.json
chunks/requirements/cbs_tender_requirements.json
```

Run:

```powershell
python pipeline.py
```

The pipeline:

1. Extracts DOCX text.
2. Creates chunks.
3. Sends chunks to Qwen3 8B.
4. Parses structured JSON.
5. Adds source metadata.
6. Saves the requirements.

### RabbitMQ consumer

The consumer waits for a RabbitMQ message, downloads the referenced S3 object, runs the extraction pipeline, builds an `RFP_ANALYSIS_COMPLETED` JSON response, publishes it to `rfp_docs_response1`, and acknowledges the request after successful processing.

Run:

```powershell
python -m app.messaging.consumer
```

Current messaging behaviour:

- Only the first document in the request's `documents` list is processed.
- A status companion event may be built locally but is not published.
- Failed analysis does not currently publish a dedicated FAILED event.
- Long-running extraction can be slow on CPU/RAM-constrained deployment VMs.

### Tests

Run:

```powershell
pytest -q
```

Latest verified local test result:

```text
21 passed
```

Test breakdown:

- 6 document-processing tests
- 11 response-builder tests
- 4 consumer tests

These are automated local tests using test fixtures and mocks where applicable. They do not, by themselves, confirm Java-side consumption or acceptance of the response schema.

## LLM Evaluation

### Qwen3 8B

The EV Charging Network tender was used for the main evaluation.

Verified results:

- 11 PDF pages
- 14 tables
- 34 chunks
- 34/34 valid JSON responses
- 143 requirements extracted
- Invalid categories: 0
- Invalid mandatory values: 0
- Invalid confidence values: 0
- Empty requirement text: 0
- Wrong source pages: 0

Qwen3 8B is the current MVP model.

Valid JSON output does not guarantee complete semantic accuracy. Further evaluation against reviewed ground-truth requirements is still needed.

### Qwen3 4B

Qwen3 4B was tested on representative chunks.

Observed:

- Chunk 3: approximately 86 seconds, invalid JSON
- Chunk 10: approximately 75 seconds, invalid JSON

The full benchmark was not completed because of slow processing and unreliable JSON output.

Qwen3 4B remains a research candidate.

## S3 and RabbitMQ Integration

The Java upload service uploads tender documents to S3. The Python consumer receives a document reference through the request queue (`RABBITMQ_QUEUE`), downloads the object from the S3 bucket/key in the message, runs the extraction pipeline, and publishes an `RFP_ANALYSIS_COMPLETED` JSON response to `rfp_docs_response1`.

The integration flow is:

```text
Java upload service
       |
       v
S3 tender document
       |
       v
RabbitMQ request queue (RABBITMQ_QUEUE)
       |
       v
Python consumer
       |
       v
S3 document download (bucket/key from message)
       |
       v
Document processing / requirement extraction
       |
       v
RFP_ANALYSIS_COMPLETED JSON
       |
       v
rfp_docs_response1  (hard-coded in publisher.py)
       |
       v
Java backend
```

Request queue: configurable via `RABBITMQ_QUEUE` (integration env commonly `copilot-analysis`; code fallback `rfp-python-test`).

Response queue: hard-coded `rfp_docs_response1` in `app/messaging/publisher.py`. Confirmed by Zeeshan as the queue consumed by the Java backend.

### Verified integration results

The CBS DOCX integration was previously tested:

- RabbitMQ request received
- S3 download successful
- 16,591 characters extracted
- 21 chunks created

A later fresh 21-chunk Qwen3 8B run timed out at chunk 9/21 after 120 seconds. The pipeline should therefore be considered subject to model-processing timeouts on longer documents and constrained VMs.

A subsequent RabbitMQ integration run successfully:

- Received a request from `copilot-analysis`
- Downloaded the requested S3 document
- Reused the 91 previously saved chunks and 266 saved requirements
- Built an `RFP_ANALYSIS_COMPLETED` response
- Published a completed response (historical note: that run targeted `rfp-python-response-test`; the current publisher target is `rfp_docs_response1`)
- Acknowledged the request

**Integration status:** Python-side request processing and publishing of `RFP_ANALYSIS_COMPLETED` to `rfp_docs_response1` match the confirmed Java consumption queue. Full frontend-to-backend end-to-end acceptance has not been claimed here. Status companion events are not published, and failed analysis does not emit a dedicated FAILED event.

The response builder currently assigns the first document's ID to requirement source metadata. Correct source mapping for requests containing multiple documents remains a limitation.

## Current Limitations

- DOCX is currently treated as one logical page; accurate physical page tracking is not implemented.
- The consumer processes only the first document in a request.
- A status companion event may be built but is not published.
- Failed analysis does not currently publish a dedicated FAILED event.
- Long-running extraction may be slow on the deployed VM due to CPU/RAM constraints.
- Source document mapping is not yet fully handled for multi-document requests.
- The response queue name is hard-coded in `app/messaging/publisher.py` (not env-configurable).
- A full Qwen3 4B run on all EV tender chunks was not completed.
- Qwen3 8B valid JSON does not mean 100% semantic accuracy.
- Some possible fragments/headings remain quality warnings.
- The existing CBS 75-requirement file is from an earlier pipeline run and does not include the newer source metadata.
- A later fresh CBS 21-chunk Qwen3 8B run timed out at chunk 9/21 after 120 seconds.
- Full frontend-to-backend end-to-end testing is not claimed as passed in this documentation.
- Scanned/image-only PDFs require OCR, which is not included in this MVP.
- The current MVP has not yet been established as production-ready.

## Documentation

- `architecture.md` — technical architecture and system design
- `evaluation.md` — measured testing and evaluation results
- `quality_evaluation.py` — automated LLM output quality checks
- `benchmark_4b.py` — Qwen3 4B benchmark
- `README.md` — project overview, setup, usage, and limitations

## Future Work

- XLSX processing
- OCR
- Improved DOCX source tracking
- RAG / vector search
- Evidence and grounding
- Requirement normalization and deduplication
- Compliance matrix generation
- Human-in-the-loop review
- Agentic RFP workflows
- Additional LLM providers
- Broader evaluation datasets
- Env-configurable response queue and dedicated FAILED / status event publishing
- Multi-document request processing
- Broader frontend-to-backend end-to-end acceptance testing
- Improved retry handling, observability, and processing-timeout management

## Security

Never commit:

- `.env`
- AWS credentials
- RabbitMQ passwords
- API keys
- Other secrets

Keep development credentials in environment variables or a secure secret-management system. Use separate development and production credentials and queues wherever possible.
