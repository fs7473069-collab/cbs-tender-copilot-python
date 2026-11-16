import functools
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from pika.exceptions import AMQPChannelError, AMQPConnectionError, AMQPError

from app.drafting.generator import generate_draft_sections, merge_preserved_drafts
from app.export.local_export import (
    LocalExportError,
    attach_export_files,
    upload_analysis_exports,
)
from app.messaging.rabbitmq import (
    configured_analysis_request_queue,
    create_connection,
)
from app.storage.s3_client import download_file
from app.orchestration.service import process_tender_document
from app.orchestration.tender_summary import summarize_tender_document
from app.response.response_builder import build_completed_response
from app.messaging.publisher import publish_message


load_dotenv()

QUEUE = configured_analysis_request_queue()
DOWNLOAD_DIR = os.path.join("data", "s3_test")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEBUG_RESPONSE_PATH = (
    PROJECT_ROOT / "local_data" / "analysis" / "actual_response.json"
)
# Durable record of requests whose completion response was confirmed by the
# broker, so a redelivery is acknowledged instead of processed and published
# twice. Holds IDs and counts only, never tender content.
COMPLETION_LEDGER_PATH = (
    PROJECT_ROOT / "local_data" / "messaging" / "completed_deliveries.json"
)
COMPLETION_LEDGER_MAX = 1000
RECONNECT_DELAY_SECONDS = 5
RECONNECT_DELAY_MAX_SECONDS = 60

ACK = "ack"
REQUEUE = "requeue"
REJECT = "reject"
# Export events (RFP_EXPORT_REQUESTED etc.) belong to the export worker.
EXPORT_EVENT_PREFIX = "RFP_EXPORT_"

_LEDGER_LOCK = threading.Lock()


def _build_completed_status_event(message, document):
    required_values = {
        "organizationId": message.get("organizationId"),
        "workspaceId": message.get("workspaceId"),
        "jobId": message.get("jobId"),
        "analysisId": message.get("analysisId"),
        "correlationId": message.get("correlationId"),
        "documentId": document.get("documentId"),
    }
    missing_fields = [
        field
        for field, value in required_values.items()
        if not isinstance(value, str) or not value.strip()
    ]
    if missing_fields:
        raise ValueError(
            "Missing required request fields: "
            + ", ".join(missing_fields)
        )

    return {
        "schemaVersion": "1.0",
        "eventType": "RFP_ANALYSIS_STATUS",
        "eventId": str(uuid4()),
        "organizationId": required_values["organizationId"],
        "workspaceId": required_values["workspaceId"],
        "jobId": required_values["jobId"],
        "analysisId": required_values["analysisId"],
        "correlationId": required_values["correlationId"],
        "attempt": message.get("attempt", 1),
        "status": "COMPLETED",
        "completedAt": (
            datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "documents": [
            {
                "documentId": required_values["documentId"],
                "status": "COMPLETED",
                "error": None,
            }
        ],
        "error": None,
    }


def outbound_completion_payload(response):
    """Copy for RabbitMQ: same completion fields, no ``result.requirements``.

    The internal response is left unchanged so exports and the debug copy
    still see every extracted requirement.
    """
    if not isinstance(response, dict):
        raise ValueError("Completed response must be an object")
    outbound = dict(response)
    outbound.pop("draftSections", None)
    result = response.get("result")
    if isinstance(result, dict):
        result_copy = dict(result)
        result_copy.pop("requirements", None)
        result_copy.pop("draftSections", None)
        outbound["result"] = result_copy
    return outbound


def _save_debug_response(response):
    """Atomically write the debug copy of the completed response."""
    DEBUG_RESPONSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = DEBUG_RESPONSE_PATH.with_suffix(
        DEBUG_RESPONSE_PATH.suffix + ".tmp"
    )
    temporary_path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(DEBUG_RESPONSE_PATH)


def _log_inbound_body(body):
    """Log size and requirement count only; never print the payload."""
    if isinstance(body, memoryview):
        raw_body = body.tobytes()
    elif isinstance(body, (bytes, bytearray)):
        raw_body = bytes(body)
    else:
        raw_body = str(body).encode("utf-8")

    print("Raw received body length (bytes):", len(raw_body))

    message = json.loads(raw_body)

    requirements = (message.get("result") or {}).get("requirements")
    if isinstance(requirements, list):
        print("result.requirements count:", len(requirements))
    else:
        print("result.requirements count: unavailable")

    return message


def _is_export_event(message):
    event_type = message.get("eventType")
    return isinstance(event_type, str) and event_type.strip().upper().startswith(
        EXPORT_EVENT_PREFIX
    )


def _delivery_key(message):
    """Idempotency key from request IDs; None when there is no jobId."""
    job_id = message.get("jobId")
    if not isinstance(job_id, str) or not job_id.strip():
        return None
    return "|".join(
        str(message.get(field) or "")
        for field in ("jobId", "analysisId", "eventId", "attempt")
    )


def _load_completion_ledger():
    try:
        data = json.loads(COMPLETION_LEDGER_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        # Unreadable ledger: duplicates are possible, so say so loudly.
        print(
            "Completion ledger unreadable; duplicate detection disabled "
            f"for this lookup: {type(exc).__name__}",
            flush=True,
        )
        return {}
    return data if isinstance(data, dict) else {}


def _find_completed_delivery(key):
    if key is None:
        return None
    with _LEDGER_LOCK:
        return _load_completion_ledger().get(key)


def _record_completed_delivery(key, response):
    if key is None:
        return
    with _LEDGER_LOCK:
        ledger = _load_completion_ledger()
        ledger[key] = {
            "jobId": response.get("jobId"),
            "responseEventId": response.get("eventId"),
            "requirementCount": len(
                (response.get("result") or {}).get("requirements") or []
            ),
            "publishedAt": (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            ),
        }
        while len(ledger) > COMPLETION_LEDGER_MAX:
            ledger.pop(next(iter(ledger)))
        COMPLETION_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = COMPLETION_LEDGER_PATH.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(COMPLETION_LEDGER_PATH)


def _evidence_status_counts(requirements):
    counts = {}
    for requirement in requirements:
        evidence_match = (
            requirement.get("evidence_match")
            if isinstance(requirement, dict) else None
        )
        status = (
            evidence_match.get("status")
            if isinstance(evidence_match, dict) else "MISSING"
        )
        counts[status] = counts.get(status, 0) + 1
    return counts


def _tender_summary(local_path, message):
    """Document-derived cover details; a failure leaves the fields null."""
    try:
        return summarize_tender_document(
            local_path, request_tender=message.get("tender")
        )
    except Exception as exc:
        print(
            "Tender summary unavailable "
            f"({type(exc).__name__}); summary fields left null."
        )
        return None


def process_delivery(body, redelivered):
    """Process one request and return how it must be settled.

    Never touches the channel, so it is safe to run off the connection thread.
    """
    try:
        message = _log_inbound_body(body)

        print("\nRFP message received:")
        print("eventType:", message.get("eventType"))
        print("jobId:", message.get("jobId"))
        print("eventId:", message.get("eventId"))
        print("redelivered:", bool(redelivered))

        if _is_export_event(message):
            print(
                "Export event received on the analysis queue; it is not an "
                "analysis request. Rejecting without requeue and without "
                "analysis or publishing. Send exports to "
                "RABBITMQ_EXPORT_REQUEST_QUEUE."
            )
            return REJECT

        delivery_key = _delivery_key(message)
        completed = _find_completed_delivery(delivery_key)
        if completed is not None:
            print(
                "Duplicate delivery: completion response already published "
                f"(responseEventId={completed.get('responseEventId')}, "
                f"publishedAt={completed.get('publishedAt')}). "
                "Acknowledging without reprocessing or republishing."
            )
            return ACK

        documents = message.get("documents", [])

        if not documents:
            raise ValueError("Request contains no documents")

        # Process the first document in the request.
        document = documents[0]

        bucket = document["bucket"]
        key = document["key"]
        filename = os.path.basename(document["filename"])

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        local_path = os.path.join(DOWNLOAD_DIR, filename)

        print("\nDownloading document from S3...")

        download_file(
            bucket=bucket,
            key=key,
            local_path=local_path,
        )

        print(f"Document downloaded to: {local_path}")

        print("\nProcessing document with the existing pipeline...")

        # Source of truth for this job: live extraction + evidence matching.
        # Do not load local_data/analysis/actual_response.json (may be stale).
        requirements = process_tender_document(local_path)

        print(f"Extracted {len(requirements)} requirements")
        print(
            "Evidence match statuses:",
            json.dumps(_evidence_status_counts(requirements), sort_keys=True),
        )

        response = build_completed_response(
            request_message=message,
            requirements=requirements,
            tender_summary=_tender_summary(local_path, message),
        )
        response_requirements = (
            (response.get("result") or {}).get("requirements") or []
        )
        try:
            generated = generate_draft_sections(
                response.get("result"),
                response_requirements,
                message.get("documents"),
            )
            response["draftSections"] = merge_preserved_drafts(
                generated,
                response.get("draftSections"),
            )
        except Exception as exc:
            print(
                "DRAFT_SECTIONS skipped "
                f"error_type={type(exc).__name__}",
                flush=True,
            )
        if len(response_requirements) != len(requirements):
            raise ValueError(
                "Completed response requirement count does not match "
                "fresh extraction results."
            )

        # Validate request IDs before uploading exports or publishing.
        status_event = _build_completed_status_event(
            message=message,
            document=document,
        )

        # Private S3 Excel/Word. Publish only after both uploads and both
        # presigned URLs succeed. A failure follows the usual requeue-once
        # policy and must not report exportFiles. Log the exception type
        # only — never the URL, bucket, or credentials.
        try:
            export_files = upload_analysis_exports(response)
            response = attach_export_files(response, export_files)
        except Exception as exc:
            print(
                "Analysis export failed "
                f"({type(exc).__name__}); completion will not be published."
            )
            raise LocalExportError("Analysis export failed") from None

        # Debug artifact keeps the full internal response, including
        # requirements. The RabbitMQ payload is a separate copy.
        _save_debug_response(response)
        outbound = outbound_completion_payload(response)

        print(f"\nResponse saved to: {DEBUG_RESPONSE_PATH.resolve()}")
        print(
            "Saved response size:",
            DEBUG_RESPONSE_PATH.stat().st_size,
            "bytes",
        )
        print("Event type:", response.get("eventType"))
        print("Job ID:", response.get("jobId"))
        print("Requirement count:", len(response_requirements))
        print("Analysis export files:", len(response.get("exportFiles") or []))

        print("\nPublishing completion response to the response queue...")
        # Publish the fresh RFP_ANALYSIS_COMPLETED payload without
        # result.requirements. Exports already used the full internal copy.
        publish_message(outbound)
        print("Completion response published successfully.")
        try:
            _record_completed_delivery(delivery_key, response)
        except OSError as exc:
            print(
                "Completion ledger could not be written "
                f"({type(exc).__name__}); a redelivery of this request "
                "would be processed and published again."
            )
        print(
            "Status companion event prepared "
            f"(not published): {status_event['eventType']}"
        )
        return ACK

    except Exception as exc:
        print(f"\nRequest processing failed: {exc}")
        if redelivered:
            print(
                "Request was already redelivered. "
                "Rejecting without requeue."
            )
            return REJECT
        print("Requeuing request for one retry.")
        return REQUEUE


def settle_delivery(ch, delivery_tag, outcome):
    """Ack/nack on the connection thread; never raises on a dead channel."""
    if not ch.is_open:
        print(
            "RabbitMQ channel is closed. "
            "No acknowledgement or rejection was attempted; "
            "the broker will redeliver the request."
        )
        return False
    try:
        if outcome == ACK:
            ch.basic_ack(delivery_tag=delivery_tag)
            print("Request acknowledged successfully.")
        else:
            ch.basic_nack(
                delivery_tag=delivery_tag,
                requeue=outcome == REQUEUE,
            )
    except AMQPError as exc:
        print(
            f"Could not {outcome} the request because RabbitMQ reported "
            f"{type(exc).__name__}: {exc}. The broker will redeliver it."
        )
        return False
    return True


def callback(ch, method, properties, body):
    """Synchronous processing and settlement (used directly by tests)."""
    outcome = process_delivery(body, method.redelivered)
    settle_delivery(ch, method.delivery_tag, outcome)


def _start_delivery_worker(connection, ch, method, body, state):
    """Run a job off the I/O thread so heartbeats keep the connection alive."""
    def run():
        started = time.perf_counter()
        outcome = process_delivery(body, method.redelivered)
        print(
            f"Request finished in {time.perf_counter() - started:.1f}s; "
            f"outcome={outcome}",
            flush=True,
        )
        try:
            connection.add_callback_threadsafe(
                functools.partial(
                    settle_delivery, ch, method.delivery_tag, outcome
                )
            )
        except AMQPError as exc:
            print(
                "RabbitMQ connection closed before settlement "
                f"({type(exc).__name__}); the broker will redeliver the "
                "request and the completion ledger prevents a duplicate "
                "publish."
            )

    worker = threading.Thread(target=run, name="rfp-delivery", daemon=True)
    state["worker"] = worker
    worker.start()


def _wait_for_worker(state):
    worker = state.get("worker")
    if worker is not None and worker.is_alive():
        # Reconnecting now would let the broker redeliver the same request
        # while it is still being processed.
        print("Waiting for the in-flight request to finish before reconnecting...")
        worker.join()
    state["worker"] = None


def _log_knowledge_base_status():
    try:
        from app.rag.retriever import TenderRetriever

        status = TenderRetriever().status()
    except Exception as exc:
        print(
            "Evidence knowledge base status: unavailable "
            f"({type(exc).__name__})"
        )
        return
    if status["available"]:
        print(
            "Evidence knowledge base status: available "
            f"chunks={status['chunks']} dimension={status['dimension']}"
        )
    else:
        print(
            "WARNING: evidence knowledge base unavailable "
            f"(code={status['error_code']}); requirements will be marked "
            "NOT_CHECKED. Build it with: python -m app.rag.ingest"
        )


def _consume(state):
    connection = create_connection()
    state["connected"] = True
    try:
        channel = connection.channel()
        channel.basic_qos(prefetch_count=1)

        request_queue = configured_analysis_request_queue()
        channel.basic_consume(
            queue=request_queue,
            on_message_callback=lambda ch, method, properties, body: (
                _start_delivery_worker(connection, ch, method, body, state)
            ),
            auto_ack=False,
        )

        print(f"Connected. Waiting for messages from '{request_queue}'...")
        print("Press Ctrl+C to stop the consumer.")

        channel.start_consuming()
    finally:
        if connection.is_open:
            try:
                connection.close()
            except AMQPError:
                pass


def start_consumer():
    _log_knowledge_base_status()
    state = {"worker": None}
    delay = RECONNECT_DELAY_SECONDS

    while True:
        try:
            _consume(state)
            return
        except KeyboardInterrupt:
            print("\nConsumer stopped by user.")
            worker = state.get("worker")
            if worker is not None and worker.is_alive():
                print(
                    "An in-flight request was not acknowledged; "
                    "the broker will redeliver it."
                )
            return
        except (AMQPConnectionError, AMQPChannelError) as exc:
            if state.pop("connected", False):
                delay = RECONNECT_DELAY_SECONDS
            print(
                f"\nRabbitMQ connection lost: {type(exc).__name__}: {exc}"
            )
            _wait_for_worker(state)
            print(f"Reconnecting in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX_SECONDS)


if __name__ == "__main__":
    start_consumer()
