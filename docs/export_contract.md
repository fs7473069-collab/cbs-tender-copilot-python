# Tender Export Contract — PROPOSED (not approved)

> **Status: PROPOSED.** This contract has not been agreed with the Java backend
> or frontend. Every field marked *Java-owned (proposed)* is an assumption made
> by the Python side and must be confirmed by Java before integration. Java
> integration is **not complete** until this contract is agreed and tested
> end-to-end.

The S3 export worker below is separate from tender analysis. It does not
change the analysis request queue (`RABBITMQ_QUEUE`) or the analysis response
queue (`rfp_docs_response1`). A **proposed** addition to
`RFP_ANALYSIS_COMPLETED` uploads Excel and Word for that analysis to the
private export bucket and returns presigned download URLs. That addition is
**not approved**. The separate `RFP_EXPORT_REQUESTED` worker is unchanged
and is not required for this analysis completion path.

## PROPOSED: `exportFiles` on `RFP_ANALYSIS_COMPLETED`

> **Status: PROPOSED. Not approved.** Java review is required before
> `exportFiles` is treated as part of the analysis contract. This does not
> replace the S3 export worker, its queues, or `RFP_EXPORT_COMPLETED`.
> The export worker still returns `artifacts` with `bucket` and `key` and
> does not generate presigned URLs. Java integration of `exportFiles` is
> **not complete**.

For the combined analysis workflow, Python renders Excel and Word from the
completed analysis already in memory, uploads both to the private S3 bucket,
and generates a presigned GET URL for each object **before** it publishes
`RFP_ANALYSIS_COMPLETED`. It does not wait for a second
`RFP_EXPORT_REQUESTED`. Rendering does not call Ollama, the retriever, or
`process_tender_document` again, and it does not re-upload the original
tender or knowledge-base documents.

The field is additive. Existing `RFP_ANALYSIS_COMPLETED` fields are unchanged.
`build_completed_response` does not add `exportFiles`. The analysis consumer
attaches it only after both uploads and both presigned URLs succeed.

When it appears: only on a completion that is actually published. Excel is
first, then Word. Each entry has `fileName`, `downloadUrl`, `bucket`, `key`,
and `expiresIn`. There is no `path`. `downloadUrl` is a presigned GET URL
for a private object. It expires. Persist `bucket` and `key`, not the URL,
if a later download is required. The bucket stays private; objects are not
given `public-read` or any ACL that makes them public.

```json
"exportFiles": [
  {
    "fileName": "tender-export.xlsx",
    "downloadUrl": "https://example.invalid/presigned-excel",
    "bucket": "export-bucket",
    "key": "exports/org-1/ws-1/job-1/analysis-1/tender-export.xlsx",
    "expiresIn": 345600
  },
  {
    "fileName": "tender-export.docx",
    "downloadUrl": "https://example.invalid/presigned-word",
    "bucket": "export-bucket",
    "key": "exports/org-1/ws-1/job-1/analysis-1/tender-export.docx",
    "expiresIn": 345600
  }
]
```

| Field | Notes |
|---|---|
| `exportFiles` | Top-level on `RFP_ANALYSIS_COMPLETED`. Absent when export failed, because that completion is not published. |
| `fileName` | Stable names: `tender-export.xlsx`, then `tender-export.docx`. No `exportId`. |
| `downloadUrl` | Presigned S3 GET URL. Expires after `expiresIn` seconds. Not a local path. |
| `bucket` | Destination bucket (`S3_EXPORT_BUCKET` or `S3_BUCKET`). Durable reference. |
| `key` | Object key. Durable reference. Same path as below. |
| `expiresIn` | Lifetime of `downloadUrl` in seconds. Default `345600` (4 days). |

Object keys:

```text
exports/{organizationId}/{workspaceId}/{jobId}/{analysisId}/tender-export.xlsx
exports/{organizationId}/{workspaceId}/{jobId}/{analysisId}/tender-export.docx
```

Segments are sanitised with the same character rules as the export worker's
S3 key segments. A segment that is empty, `.`, or `..`, or that contains a
`..` path component, fails the export. Nothing is published in that case.
The next successful run uses the same keys and overwrites the objects.

**Bucket.** This flow has no export-request `output.bucket`. Resolution
matches the export worker when `output.bucket` is absent: `S3_EXPORT_BUCKET`,
else `S3_BUCKET`. If neither is set, the export fails and the completion is
not published. Logs may name the source (`S3_EXPORT_BUCKET` or `S3_BUCKET`).
They do not include the bucket value, credentials, or presigned URLs.

| Env var | Default | Purpose |
|---|---|---|
| `S3_EXPORT_BUCKET` | *(none)* | Bucket for these analysis exports |
| `S3_BUCKET` | *(none)* | Fallback when `S3_EXPORT_BUCKET` is unset or blank |
| `S3_PRESIGNED_URL_EXPIRES_SECONDS` | `345600` | Presigned GET lifetime in seconds (4 days). Unset or blank uses the default. Any other value must be a positive integer; otherwise the export fails. AWS SigV4 allows at most 7 days when signing with IAM user access keys. Temporary STS/role credentials cannot mint a URL longer than the remaining session (often 12 hours or less); a 4-day URL is not guaranteed in that case. |
| `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | as analysis | S3 client (`create_s3_client`) |

`ContentType` values match the export worker:
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` and
`application/vnd.openxmlformats-officedocument.wordprocessingml.document`.

This path does not write `local_data/exports`. A debug copy of the published
JSON may still be saved under `local_data/analysis`; that file is not a
download location for Java.

The analysis pipeline has no human decisions or approved drafts. The snapshot
passed to the existing renderers uses tender fields and formatted requirements
from the completion payload and sets `requirementId` from `sourceItemKey`.
Draft sections stay empty. Suggested `actions` are derived from evidence
status (verify retrieved evidence, collect evidence for mandatory
`NOT_FOUND`, or retry `NOT_CHECKED`). They are marked `AI_SUGGESTED`, are
not published on the analysis JSON, and do not invent owners or due dates.
Final Decision is never invented. `aiRecommendation` on that snapshot is a
deterministic map from evidence-match status only (`NEEDS_HUMAN_REVIEW`,
`EVIDENCE_REQUIRED`, or `NOT_CHECKED`) and is never a COMPLIANT /
NON_COMPLIANT decision. Word states that no approved sections are available.

If rendering, upload, or presigned URL generation fails, Python does **not**
publish `RFP_ANALYSIS_COMPLETED`, does **not** include a partial
`exportFiles`, and does **not** acknowledge the analysis message as success.
The analysis consumer's existing failure policy applies: requeue once, then
reject on redelivery, so resume can reuse extraction progress. The log
records the exception type only. If the Excel object was uploaded and the
Word upload or either URL then failed, the Excel object may remain in the
bucket; the next successful run overwrites the same keys.

A duplicate delivery that is already in the completion ledger is acknowledged
without rendering, uploading, or publishing again.

The separate worker (`python -m app.messaging.export_consumer`) is unchanged
and is not used by this analysis completion path.

```text
Java ──RFP_EXPORT_REQUESTED──> RABBITMQ_EXPORT_REQUEST_QUEUE
                                     │
                         python -m app.messaging.export_consumer
                         render Excel + Word from the snapshot (no LLM/RAG)
                         upload both to S3
                                     │
Java <──RFP_EXPORT_COMPLETED / RFP_EXPORT_FAILED── RABBITMQ_EXPORT_RESPONSE_QUEUE
```

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `RABBITMQ_EXPORT_REQUEST_QUEUE` | `rfp-export-requests` | Export request queue (consumed) |
| `RABBITMQ_EXPORT_RESPONSE_QUEUE` | `rfp-export-responses` | Completion/failure queue (published) |
| `RABBITMQ_QUEUE` | `rfp-python-test` | Analysis request queue; read only to refuse a clash (see below) |
| `S3_EXPORT_BUCKET` | *(none)* | Bucket used when the request has no `output.bucket` |
| `S3_BUCKET` | *(none)* | Fallback when `S3_EXPORT_BUCKET` is unset or blank |
| `EXPORT_REQUEUE_DELAY_SECONDS` | `5` | Wait before a retryable failure is requeued (0–300; invalid → default) |
| `RABBITMQ_HOST`, `RABBITMQ_PORT`, `RABBITMQ_VHOST`, `RABBITMQ_USERNAME`, `RABBITMQ_PASSWORD` | as analysis | Broker connection (shared `create_connection`) |
| `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | as analysis | S3 client (shared `create_s3_client`) |

### Startup checks (before connecting to RabbitMQ)

The worker exits without connecting when:

- an export queue name is blank;
- `RABBITMQ_EXPORT_REQUEST_QUEUE` equals `RABBITMQ_EXPORT_RESPONSE_QUEUE`;
- either export queue equals the analysis request queue (`RABBITMQ_QUEUE`,
  default `rfp-python-test`) or the analysis response queue
  (`rfp_docs_response1`). Names are compared exactly after trimming
  (RabbitMQ queue names are case-sensitive).

Exit code `2` = configuration error; `3` = export ledger unavailable (see
[Delivery semantics](#delivery-semantics)). It logs which source supplies the
default bucket (`S3_EXPORT_BUCKET`, `S3_BUCKET` or none), never the bucket
credentials.

### Queue declaration (Java must confirm)

Python declares both export queues with `queue_declare(queue=..., durable=True)`
and **no other arguments**, on the default exchange (routing key = queue name).
Responses are published persistent (`delivery_mode=2`), `mandatory=True`, with
publisher confirms. If Java or ops pre-declare a queue with different
arguments, RabbitMQ refuses the declaration (`PRECONDITION_FAILED`) and the
worker cannot start, so the arguments must be agreed first.

Python does **not** set any of the following; they are open questions only:

- dead-letter exchange / routing key (`x-dead-letter-exchange`, `x-dead-letter-routing-key`);
- queue type (`x-queue-type: quorum` vs classic);
- message TTL / queue length limits (`x-message-ttl`, `x-max-length`);
- a delayed-retry queue or delayed-message exchange.

## Request: `RFP_EXPORT_REQUESTED`

```json
{
  "schemaVersion": "1.0",
  "eventType": "RFP_EXPORT_REQUESTED",
  "eventId": "8f0c1a3e-0000-0000-0000-000000000001",
  "exportId": "exp-0001",
  "organizationId": "org-1",
  "workspaceId": "ws-1",
  "tenderId": "tender-1",
  "correlationId": "corr-1",
  "attempt": 1,
  "requestedBy": "user-1",
  "requestedAt": "2026-11-05T10:00:00Z",
  "output": {
    "bucket": "example-export-bucket",
    "keyPrefix": "exports/org-1/ws-1/tender-1/exp-0001/"
  },
  "snapshot": {
    "tender": {
      "title": "Example tender",
      "customerName": "Example customer",
      "tenderReference": "REF-1",
      "submissionDeadline": "2026-12-01T12:00:00Z"
    },
    "requirements": [
      {
        "requirementId": "req-uuid-1",
        "sourceItemKey": "REQ-001",
        "content": "Supplier must provide 24/7 support.",
        "mandatory": true,
        "category": "Support",
        "confidence": 0.9,
        "sources": [
          {
            "documentId": "doc-1",
            "filename": "tender.pdf",
            "pageNo": 4,
            "section": "3.2",
            "clause": "3.2.1",
            "sheet": null,
            "cellRange": null,
            "excerpt": "The supplier shall provide 24/7 support.",
            "sourceRole": "PRIMARY",
            "confidence": 0.9
          }
        ],
        "evidenceMatch": {
          "status": "REVIEW_REQUIRED",
          "note": "Retrieved evidence requires human verification.",
          "evidence": [
            {"text": "...", "source": "support-policy.pdf", "page": 2, "similarity": 0.82}
          ]
        },
        "aiRecommendation": null,
        "decision": {
          "status": "COMPLIANT",
          "decidedBy": "user-2",
          "decidedAt": "2026-11-04T15:00:00Z",
          "comment": "Covered by standard SLA."
        }
      }
    ],
    "actions": [
      {
        "actionId": "act-1",
        "requirementId": "req-uuid-1",
        "title": "Confirm SLA wording",
        "description": "Check SLA appendix matches clause 3.2.1.",
        "owner": "user-3",
        "dueDate": "2026-11-20",
        "status": "OPEN"
      }
    ],
    "draftSections": [
      {
        "sectionId": "sec-1",
        "title": "Executive Summary",
        "content": "First paragraph.\n\nSecond paragraph.",
        "approvalStatus": "APPROVED",
        "order": 1
      }
    ]
  }
}
```

### Field definitions

| Field | Required | Owner | Notes |
|---|---|---|---|
| `eventType` | yes | Java | Must be exactly `RFP_EXPORT_REQUESTED`. |
| `exportId` | yes | Java-owned (proposed) | Idempotency key; one export = one `exportId`. |
| `organizationId`, `workspaceId`, `tenderId` | yes | Java | Echoed in responses; used for the default S3 key. |
| `eventId`, `correlationId` | no | Java | Echoed (`eventId` as `requestEventId`); `null` when absent or blank. |
| `attempt` | no | Java | Echoed when it is a JSON integer; `null` when absent or not an integer. Python does not count attempts. |
| `requestedBy`, `requestedAt`, `schemaVersion` | no | Java-owned (proposed) | Not used by Python. |
| `output.bucket` | no | Java-owned (proposed) | Wins over `S3_EXPORT_BUCKET` / `S3_BUCKET`. A blank value is rejected (`INVALID_OUTPUT`), not replaced by the env bucket. |
| `output.keyPrefix` | no | Java-owned (proposed) | Segments are sanitised to `[A-Za-z0-9._-]`; `.`/`..` segments are rejected. |
| `snapshot` | yes | Java-owned (proposed) | Only source of export content. Inline JSON. |
| `snapshot.tender.*` | no | Java | Missing fields are omitted from Word. |
| `snapshot.requirements[]` | yes (may be empty) | Java | Each item needs non-empty `content` and `requirementId` or `sourceItemKey`. |
| `requirements[].requirementId` | recommended | Java-owned (proposed) | Persistent ID. The analysis `sourceItemKey` (`REQ-001`) is positional and not stable across re-analysis. |
| `requirements[].mandatory` | no | Java | `true`/`false`/`null`; `null` renders as "Not provided". |
| `requirements[].sources[]`, `evidenceMatch` | no | Python analysis output, relayed by Java | Same shape as `RFP_ANALYSIS_COMPLETED`; `filename` is an optional addition. |
| `requirements[].aiRecommendation` | no | Java-owned on `RFP_EXPORT_REQUESTED`; analysis Excel snapshot only | String or `{recommendation, rationale}`. Analysis completion Excel derives `{recommendation, rationale}` from evidence status after matching. Not added to published `RFP_ANALYSIS_COMPLETED` requirements. Blank Java snapshots still render as "Not provided". Never a Final Decision. |
| `requirements[].decision` | no | Java-owned (proposed) | Human decision. Only the four decision values below count; anything else is no decision ("No decision"). |
| `snapshot.actions[]` | no | Java-owned on `RFP_EXPORT_REQUESTED`; analysis Excel/Word snapshot may add `AI_SUGGESTED` rows | Java actions rendered as-is. Analysis completion may append suggested actions from evidence status; those are not published on `RFP_ANALYSIS_COMPLETED`. |
| `snapshot.draftSections[]` | no | Java-owned on `RFP_EXPORT_REQUESTED`; analysis Word snapshot may include Python `DRAFT` sections | Only `approvalStatus` == `APPROVED` is exported to Word. Analysis may generate six bid-response drafts (`status`/`approvalStatus` `DRAFT`) after evidence matching. They are not published on `RFP_ANALYSIS_COMPLETED`. Java must persist, edit, and approve. Python never self-approves. |

**Proposed decision values** (Java to confirm): `COMPLIANT`,
`PARTIALLY_COMPLIANT`, `NON_COMPLIANT`, `NOT_APPLICABLE`. This is a fixed
allowlist in code (`app.export.snapshot.DECISION_VALUES`), not
env-configurable.

- Matching trims surrounding whitespace and ignores case
  (`" compliant "` → `COMPLIANT`); the Excel shows the canonical upper-case
  value.
- Matching is otherwise strict: spaces or hyphens instead of underscores
  (`PARTIALLY COMPLIANT`, `NON-COMPLIANT`) are **not** accepted.
- Missing `decision`, missing/`null`/blank `status`, non-string `status`,
  `UNKNOWN`, `TBD`, `N/A`, `PENDING`, `NO_DECISION`, `UNDECIDED` and every
  other unrecognised value mean **no decision**: "Final Decision" shows
  `No decision` and the raw value is not displayed anywhere. Such
  requirements count towards `warnings.mandatoryWithoutDecision` when
  `mandatory: true`.
- `decidedBy`, `decidedAt` and `comment` are rendered as supplied, even when
  the status is not a valid decision.

**Proposed approval values** (Java to confirm): `DRAFT`, `PENDING`,
`APPROVED`, `REJECTED`. Anything other than `APPROVED` (including missing,
`null` and unknown values) is excluded.

AI evidence status (`REVIEW_REQUIRED` / `NOT_FOUND` / `NOT_CHECKED`) is **not**
a compliance decision. The Excel "Final Decision" column is never derived from
evidence status or AI recommendation.

## Outputs

**Excel** (`tender-export-{exportId}.xlsx`) — exactly three sheets, in order:

1. `Requirements` — Requirement ID, Source Item Key, Requirement, Mandatory,
   Category, Confidence, Source Count, Source Document, Source Page, Source
   Section, Source Clause, Source Sheet, Source Cell Range, Source Excerpt.
   One row per requirement; multiple sources are one line each, prefixed
   `[1]`, `[2]`, … in the same order across source columns.
2. `Compliance Matrix` — Requirement ID, Requirement, Mandatory, AI Evidence
   Status, AI Evidence Note, Evidence References, AI Recommendation, Final
   Decision, Decided By, Decided At, Decision Comment.
3. `Actions` — Action ID, Requirement ID, Title, Description, Owner, Due Date,
   Status. Headers are always present, even with no actions.

All cells are written as text (cell type string, never formulas or numbers).

- **Formula injection:** a value is prefixed with `'` when it starts with tab
  or CR, or when its first character after leading whitespace (including
  Unicode whitespace such as NBSP/ideographic space, and zero-width
  characters/BOM) is `=`, `+`, `-`, `@` or a full-width/small-form variant
  (`＝ ＋ － ＠ ﹦ ﹢ ﹣`). Exception: a value that is exactly a plain decimal
  number — optional sign, ASCII digits, optional `,`-grouped thousands,
  optional decimal part, no spaces (e.g. `-5`, `-3.14`, `-1,234.50`, `+7`) — is
  written unchanged as text. `-1+1`, `-1e5`, ` -5` and `-cmd|' /C calc'!A0` are
  still neutralised. Finite JSON numbers (e.g. `confidence: -0.5`) are written
  as their plain text form without a prefix.
- **Invalid XML characters** are removed (both Excel and Word): C0 controls
  except tab/LF/CR, lone UTF-16 surrogates, and U+FFFE/U+FFFF. A split but
  valid surrogate pair is recombined, not dropped.
- **Length:** Excel's 32,767 limit is counted in UTF-16 code units (an emoji
  counts as 2; openpyxl itself only counts Python characters). Longer values
  are cut to fit, never splitting a surrogate pair, and end with
  ` [truncated]`.

**Word** (`tender-export-{exportId}.docx`) — structured tender-response layout:
cover (CBS banner, title, customer, reference, human-readable deadline), a
static table of contents (ordinary table text, not a Word TOC field). Page
numbers are copied from a LibreOffice PDF layout of the same file when
`soffice` is available; otherwise the Page column is "—" rather than an
estimate,
executive summary, understanding of requirements, proposed technical
approach, implementation methodology, company experience, a landscape
requirement-by-requirement compliance response, assumptions, conclusion,
then landscape appendices for extracted requirements and evidence
references. Narrative chapters
include Java `draftSections` only when `approvalStatus` is `APPROVED`
(trimmed, case-insensitive) and the section title matches that chapter
(exact or prefix). Unmatched approved titles appear under "Approved Draft
Sections" only when such titles exist, ordered by numeric `order` then
input order. Missing approved narrative is labelled "Information Required"
once per chapter; if no approved drafts exist the document states that
once. Python does not invent company experience or commitments. AI
evidence status is never treated as a human decision. Content is plain
text; blank lines separate paragraphs.

## S3

- Bucket precedence: `output.bucket`, else `S3_EXPORT_BUCKET`, else
  `S3_BUCKET`; otherwise the export fails with `CONFIGURATION_ERROR`. The
  `S3_BUCKET` fallback is the same variable name used in the deployment
  `.env`. Analysis document download still takes the tender bucket and key
  from each analysis request. The proposed analysis export above uses this
  same env resolution when there is no `output.bucket`. Using `S3_BUCKET`
  means exports land in the same bucket as uploaded tender documents, under
  the separate `exports/` prefix — set `S3_EXPORT_BUCKET` if exports must
  be isolated.
- **No bucket allowlist:** any `output.bucket` that the configured S3
  credentials can write to is accepted. Whether Python should restrict
  buckets (allowlist) or Java should stop sending `output.bucket` is a
  Java/ops decision; the IAM policy of the S3 credentials is the current
  boundary.
- Keys: `{keyPrefix}/tender-export-{exportId}.xlsx|.docx`; default prefix
  `exports/{organizationId}/{workspaceId}/{tenderId}/{exportId}/`.
- **ID sanitisation for keys/filenames:** each path segment (and the
  `exportId` in the filename) is trimmed, every character outside
  `[A-Za-z0-9._-]` is replaced by `_`, and it is cut to 200 characters; a
  segment that ends up empty, `.` or `..` is rejected (`INVALID_OUTPUT`). This
  is lossy: e.g. `exp 1`, `exp/1` and `exp_1` all become `exp_1`, and IDs that
  only differ after 200 characters collide, so two different exports could
  overwrite the same object. The ledger and events still use the unmodified
  `exportId`. Java should use IDs already in `[A-Za-z0-9._-]` (e.g. UUIDs).
- `ContentType`: `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`
  and `application/vnd.openxmlformats-officedocument.wordprocessingml.document`.
- This export worker does not generate presigned URLs. Java should generate
  short-lived download URLs from `bucket`/`key` on `RFP_EXPORT_COMPLETED`.
  The proposed analysis `exportFiles.downloadUrl` is a separate, unapproved
  field and is not part of this worker's completion contract.

## Completion: `RFP_EXPORT_COMPLETED`

Published only after **both** uploads succeeded; the request is acknowledged
only after the broker confirmed this publish.

```json
{
  "schemaVersion": "1.0",
  "eventType": "RFP_EXPORT_COMPLETED",
  "eventId": "generated-uuid",
  "exportId": "exp-0001",
  "organizationId": "org-1",
  "workspaceId": "ws-1",
  "tenderId": "tender-1",
  "correlationId": "corr-1",
  "requestEventId": "8f0c1a3e-0000-0000-0000-000000000001",
  "attempt": 1,
  "status": "COMPLETED",
  "completedAt": "2026-11-05T10:00:05Z",
  "artifacts": [
    {
      "type": "EXCEL",
      "bucket": "example-export-bucket",
      "key": "exports/org-1/ws-1/tender-1/exp-0001/tender-export-exp-0001.xlsx",
      "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      "sizeBytes": 7012,
      "sha256": "hex"
    },
    {
      "type": "WORD",
      "bucket": "example-export-bucket",
      "key": "exports/org-1/ws-1/tender-1/exp-0001/tender-export-exp-0001.docx",
      "contentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      "sizeBytes": 36650,
      "sha256": "hex"
    }
  ],
  "warnings": {
    "mandatoryWithoutDecision": 0,
    "approvedSectionCount": 1,
    "excludedSectionCount": 0
  },
  "counts": {"requirements": 1, "actions": 1},
  "error": null
}
```

`warnings.mandatoryWithoutDecision` counts requirements with
`mandatory: true` and no decision (as defined above). It is informational; the
export is not blocked.

`completedAt` / `failedAt` (and the ledger's `updatedAt`) are ISO-8601 UTC
with seconds precision and a trailing `Z` (`YYYY-MM-DDTHH:MM:SSZ`), no
fractional seconds.

## Failure: `RFP_EXPORT_FAILED`

```json
{
  "schemaVersion": "1.0",
  "eventType": "RFP_EXPORT_FAILED",
  "eventId": "generated-uuid",
  "exportId": "exp-0001",
  "organizationId": "org-1",
  "workspaceId": "ws-1",
  "tenderId": "tender-1",
  "correlationId": "corr-1",
  "requestEventId": "8f0c1a3e-0000-0000-0000-000000000001",
  "attempt": 1,
  "status": "FAILED",
  "failedAt": "2026-11-05T10:00:05Z",
  "artifacts": [],
  "error": {
    "code": "UPLOAD_FAILED",
    "message": "S3 upload failed (ClientError)",
    "retryable": true
  }
}
```

Missing correlation fields are `null` (also when a value contains characters
that cannot be encoded as UTF-8, e.g. a lone surrogate).

**`error.message` privacy rule:** a fixed, code-level sentence. It may name
contract fields or structural JSON paths (e.g. `Missing required request
fields: tenderId`, `snapshot.requirements[3].content must be a non-empty
string`) and an exception *class* name (e.g. `S3 upload failed
(ClientError)`). It never contains values from the request or snapshot
(requirement text, draft content, IDs, bucket names), credentials, or raw
exception/S3/boto text. Worker logs follow the same rule; they include only
`exportId`, event types, codes, byte sizes and the bucket *source*.

| `error.code` | Retryable | Python behaviour |
|---|---|---|
| `MALFORMED_JSON` | no | No event (nothing to correlate); reject, no requeue. |
| `INVALID_EVENT_TYPE`, `MISSING_REQUIRED_FIELDS`, `INVALID_FIELD_VALUE`, `INVALID_SNAPSHOT`, `INVALID_OUTPUT`, `CONFIGURATION_ERROR`, `RENDER_FAILED` | no | Publish FAILED (if `exportId` usable), reject without requeue. |
| `EXPORT_ID_CONFLICT` | no | The `exportId` was already COMPLETED with a different snapshot hash. Nothing is re-uploaded or overwritten; publish FAILED, reject without requeue. Use a new `exportId`. |
| `UPLOAD_FAILED`, `PUBLISH_FAILED`, `INTERNAL_ERROR` | yes | First delivery: requeue (after `EXPORT_REQUEUE_DELAY_SECONDS`), no event. Redelivery: publish FAILED, reject without requeue. |

Data problems (invalid characters in IDs, invalid snapshot, rendering errors)
are never `retryable`. Invalid characters inside snapshot *text* are cleaned
(see Outputs) and do not fail the export. `INTERNAL_ERROR` is reserved for
unexpected bugs and stays retryable.

If the FAILED event itself cannot be published, a first delivery is requeued
once; a redelivery is rejected.

### Retry delay

A retryable failure is requeued with `basic_nack(requeue=True)` after the
worker thread waits `EXPORT_REQUEUE_DELAY_SECONDS` (default 5 s, max 300 s).
The wait runs on the worker thread, not the connection thread, so heartbeats
continue; with prefetch 1 the worker takes no other export meanwhile. The
message returns to its original queue position and is redelivered at once
after the wait — there is only **one** retry (on redelivery the request is
failed). For real backoff (longer or multiple delays) the recommended
mitigation is a broker-side delayed retry (dead-letter queue with TTL, or the
delayed-message exchange), which must be designed and confirmed by Java/ops.

## Delivery semantics

- Prefetch 1, manual ack, processing on a worker thread with thread-safe
  settlement, reconnect with backoff (same pattern as the analysis consumer).
- Idempotency by `exportId` via `local_data/messaging/completed_exports.json`
  (IDs, S3 keys and hashes only — no tender content):
  - `COMPLETED` + same snapshot hash (SHA-256 of the canonical snapshot JSON):
    a redelivery/resend is acknowledged without re-uploading or republishing.
  - `COMPLETED` + different snapshot hash: `EXPORT_ID_CONFLICT` (see above).
  - `UPLOADED` (uploads done, completion publish failed): the retry reuses the
    uploaded artifacts when the snapshot hash is unchanged; with a changed
    snapshot it renders and uploads again (overwriting the same keys), since
    no completion was ever published.
  - Failures are not recorded, so Java may resend the same `exportId` after a
    FAILED event. A new export should normally use a new `exportId`.
  - The newest 1,000 entries are kept.
- **Unreadable ledger:** a missing file is a normal, empty ledger. Any other
  read problem (invalid JSON or UTF-8, top-level not an object, an entry not
  an object, OS/permission error) is never silently replaced:
  - the original file is left untouched and a copy is written next to it as
    `completed_exports.json.corrupt-<UTC timestamp>` (when it can be read);
  - at startup the worker exits with code `3` before connecting;
  - during processing the request is **requeued unprocessed** (no upload, no
    COMPLETED or FAILED event) and the worker stops consuming and exits with
    code `3`. If the ledger becomes unreadable right after COMPLETED was
    published, the request is acknowledged (completion already sent) and the
    worker stops.

  Rationale: processing without the ledger loses duplicate protection
  (double uploads/COMPLETED events), and requeuing while still consuming
  would spin the same message in a tight loop. Stopping keeps the message
  safe in the queue and forces an operator fix. Under a restart policy the
  worker keeps exiting at the startup check, without consuming, until the
  file is repaired or moved aside.
- Writes are atomic (temporary file + rename). Reads and read-modify-write are
  serialised by an in-process lock plus an `fcntl` lock on
  `completed_exports.json.lock` (POSIX only; on Windows only the in-process
  lock applies).
- **Multi-worker limitation:** the ledger is local to one host/volume. The
  file lock only covers processes sharing that filesystem, and the check →
  upload → publish → record sequence is not one atomic step, so two workers
  handling the same `exportId` at the same moment could both upload and
  publish. Replicas with separate volumes do not share the ledger at all.
  Run a single export worker until a shared idempotency store (or Java-side
  de-duplication of COMPLETED events by `exportId`) is agreed.
- The analysis consumer rejects (no requeue, no analysis, no publish) any
  message whose `eventType` starts with `RFP_EXPORT_`.

## Open questions for Java / frontend

1. Decision status values; whether strict matching (trim + case-insensitive,
   underscores required) is right; whether `decidedBy`/`decidedAt`/`comment`
   should be hidden when the status is not a valid decision.
2. Approval model and values for draft sections; who approves; same strict
   matching (`APPROVED` only).
3. Draft content format (plain text assumed; Markdown/HTML/rich text?).
4. Persistent requirement IDs (`requirementId`) versus positional `sourceItemKey`.
5. Bucket and key prefix ownership; whether Python should enforce a bucket
   allowlist; whether the `S3_BUCKET` fallback is acceptable.
6. Presigned URLs on `RFP_EXPORT_COMPLETED`: this worker still returns
   `bucket`/`key` only. The proposed analysis `downloadUrl` expires and is
   not a permanent link; Java must not store it as one. Not approved.
7. Snapshot size limits; inline snapshot vs an S3 reference for large tenders.
8. Whether AI recommendations will exist and their shape.
9. Queue names and declaration arguments (durable only today; DLX, quorum,
   TTL, max-length?) and whether failures should go to a dead-letter queue.
10. Retry strategy: single immediate-after-delay requeue today vs a
    broker-side delayed retry queue.
11. Multi-replica idempotency: shared ledger/store, or Java de-duplicates by
    `exportId`; `exportId` character set (to avoid sanitisation collisions).
12. Meaning of `attempt` (Java-owned counter; Python only echoes it).
13. **PROPOSED, not approved:** `exportFiles` on `RFP_ANALYSIS_COMPLETED`
    (`fileName`, expiring `downloadUrl`, durable `bucket`/`key`, and
    `expiresIn`). Java integration is not complete. See the section above.

## Running the worker

```bash
python -m app.messaging.export_consumer
```

or, with Docker Compose (optional profile; the analysis `python-agent`
service and the Dockerfile `CMD` are unchanged):

```bash
docker compose --profile export up -d export-worker
```

The compose service reuses `.env` and persists the ledger in the
`export_ledger` volume (`/app/local_data/messaging`).
