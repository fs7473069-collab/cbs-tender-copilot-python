"""RabbitMQ worker for tender exports (Excel + Word to S3).

Runs separately from the analysis consumer:

    python -m app.messaging.export_consumer

Consumes RFP_EXPORT_REQUESTED from RABBITMQ_EXPORT_REQUEST_QUEUE and
publishes RFP_EXPORT_COMPLETED / RFP_EXPORT_FAILED to
RABBITMQ_EXPORT_RESPONSE_QUEUE. Never calls Ollama, RAG or the analysis
pipeline; rendering uses only the snapshot in the request.
"""

import functools
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from pika.exceptions import AMQPChannelError, AMQPConnectionError, AMQPError

from app.export import (
    SnapshotError,
    approved_sections,
    count_mandatory_without_decision,
    normalize_snapshot,
    render_excel,
    render_word,
)
from app.messaging.publisher import RESPONSE_QUEUE as ANALYSIS_RESPONSE_QUEUE
from app.messaging.publisher import publish_to_queue
from app.messaging.rabbitmq import create_connection
from app.storage.s3_client import upload_bytes

try:
    import fcntl
except ImportError:  # Windows: only the in-process lock is available.
    fcntl = None

load_dotenv()

EXPORT_REQUESTED = "RFP_EXPORT_REQUESTED"
EXPORT_COMPLETED = "RFP_EXPORT_COMPLETED"
EXPORT_FAILED = "RFP_EXPORT_FAILED"
SCHEMA_VERSION = "1.0"

REQUEST_QUEUE = os.getenv("RABBITMQ_EXPORT_REQUEST_QUEUE", "rfp-export-requests")
RESPONSE_QUEUE = os.getenv(
    "RABBITMQ_EXPORT_RESPONSE_QUEUE", "rfp-export-responses"
)
# Must match the fallback in app.messaging.consumer.
ANALYSIS_REQUEST_QUEUE_ENV = "RABBITMQ_QUEUE"
ANALYSIS_REQUEST_QUEUE_DEFAULT = "rfp-python-test"

EXPORT_BUCKET_ENV = "S3_EXPORT_BUCKET"
FALLBACK_BUCKET_ENV = "S3_BUCKET"

REQUEUE_DELAY_ENV = "EXPORT_REQUEUE_DELAY_SECONDS"
REQUEUE_DELAY_DEFAULT_SECONDS = 5.0
REQUEUE_DELAY_MAX_SECONDS = 300.0

XLSX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Separate from the analysis ledger. Holds IDs, keys and hashes only.
EXPORT_LEDGER_PATH = (
    PROJECT_ROOT / "local_data" / "messaging" / "completed_exports.json"
)
EXPORT_LEDGER_MAX = 1000
RECONNECT_DELAY_SECONDS = 5
RECONNECT_DELAY_MAX_SECONDS = 60

EXIT_CONFIGURATION_ERROR = 2
EXIT_LEDGER_UNAVAILABLE = 3

REQUIRED_ID_FIELDS = ("exportId", "organizationId", "workspaceId", "tenderId")
CORRELATION_FIELDS = (
    "exportId", "organizationId", "workspaceId", "tenderId", "correlationId",
)
_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]")
MAX_SEGMENT_LENGTH = 200

ACK = "ack"
REQUEUE = "requeue"
REJECT = "reject"
# Settle, then stop consuming: the ledger cannot guarantee idempotency.
REQUEUE_AND_STOP = "requeue_and_stop"
ACK_AND_STOP = "ack_and_stop"
_STOP_OUTCOMES = {REQUEUE_AND_STOP: REQUEUE, ACK_AND_STOP: ACK}

_LEDGER_LOCK = threading.Lock()


class ExportRequestError(Exception):
    """A request that cannot succeed on retry (bad input or configuration)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class TransientExportError(Exception):
    """A failure that may succeed on retry (S3 upload, broker publish)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ExportConfigurationError(Exception):
    """Worker configuration that must be fixed before it may connect."""


class LedgerUnavailableError(Exception):
    """The idempotency ledger cannot be read safely; do not process."""


def _now():
    """ISO-8601 UTC with seconds precision, e.g. 2026-11-05T10:00:05Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_present(value):
    return isinstance(value, str) and bool(value.strip())


def _is_encodable(value):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _echo(value):
    """A request string safe to copy into a response event, else None."""
    return value if _is_present(value) and _is_encodable(value) else None


# Configuration -----------------------------------------------------------------

def validate_queue_configuration(
    request_queue,
    response_queue,
    analysis_request_queue,
    analysis_response_queue=ANALYSIS_RESPONSE_QUEUE,
):
    """Refuse queue settings that would mix export and analysis traffic."""
    blank = [
        name
        for name, value in (
            ("RABBITMQ_EXPORT_REQUEST_QUEUE", request_queue),
            ("RABBITMQ_EXPORT_RESPONSE_QUEUE", response_queue),
        )
        if not _is_present(value)
    ]
    if blank:
        raise ExportConfigurationError(
            "Queue name must not be blank: " + ", ".join(blank)
        )
    export_queues = (
        ("RABBITMQ_EXPORT_REQUEST_QUEUE", request_queue.strip()),
        ("RABBITMQ_EXPORT_RESPONSE_QUEUE", response_queue.strip()),
    )
    others = [
        ("RABBITMQ_EXPORT_RESPONSE_QUEUE", response_queue.strip()),
        (f"{ANALYSIS_REQUEST_QUEUE_ENV} (analysis request queue)",
         (analysis_request_queue or "").strip()),
        ("analysis response queue", (analysis_response_queue or "").strip()),
    ]
    problems = []
    for name, value in export_queues:
        for other_name, other_value in others:
            if other_name != name and value == other_value:
                problems.append(f"{name} must differ from {other_name} ('{value}')")
    if problems:
        raise ExportConfigurationError("; ".join(problems))


def _configured_analysis_request_queue():
    return os.getenv(ANALYSIS_REQUEST_QUEUE_ENV, ANALYSIS_REQUEST_QUEUE_DEFAULT)


def requeue_delay_seconds():
    raw = os.getenv(REQUEUE_DELAY_ENV)
    if raw is None or not raw.strip():
        return REQUEUE_DELAY_DEFAULT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        print(f"{REQUEUE_DELAY_ENV} is not a number; using the default.")
        return REQUEUE_DELAY_DEFAULT_SECONDS
    if value != value or value < 0:
        return REQUEUE_DELAY_DEFAULT_SECONDS
    return min(value, REQUEUE_DELAY_MAX_SECONDS)


def default_bucket():
    """(bucket, source env var name) used when a request has no output.bucket."""
    for env_name in (EXPORT_BUCKET_ENV, FALLBACK_BUCKET_ENV):
        value = os.getenv(env_name)
        if _is_present(value):
            return value.strip(), env_name
    return None, None


# Request handling ----------------------------------------------------------------

def parse_body(body):
    if isinstance(body, memoryview):
        body = body.tobytes()
    if isinstance(body, str):
        body = body.encode("utf-8", "surrogatepass")
    print("Raw received body length (bytes):", len(body))
    try:
        message = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExportRequestError(
            "MALFORMED_JSON", f"Request body is not valid JSON ({type(exc).__name__})"
        ) from None
    if not isinstance(message, dict):
        raise ExportRequestError("MALFORMED_JSON", "Request body must be a JSON object")
    return message


def validate_request(message):
    """Return the normalised snapshot; raise ExportRequestError otherwise."""
    if message.get("eventType") != EXPORT_REQUESTED:
        raise ExportRequestError(
            "INVALID_EVENT_TYPE",
            f"eventType must be {EXPORT_REQUESTED}",
        )
    missing = [f for f in REQUIRED_ID_FIELDS if not _is_present(message.get(f))]
    if missing:
        raise ExportRequestError(
            "MISSING_REQUIRED_FIELDS",
            "Missing required request fields: " + ", ".join(missing),
        )
    invalid = [
        f for f in CORRELATION_FIELDS + ("eventId",)
        if _is_present(message.get(f)) and not _is_encodable(message[f])
    ]
    if invalid:
        raise ExportRequestError(
            "INVALID_FIELD_VALUE",
            "Request fields contain invalid characters: " + ", ".join(invalid),
        )
    output = message.get("output")
    if output is not None and not isinstance(output, dict):
        raise ExportRequestError("INVALID_OUTPUT", "output must be an object or null")
    try:
        return normalize_snapshot(message.get("snapshot"))
    except SnapshotError as exc:
        raise ExportRequestError("INVALID_SNAPSHOT", str(exc)) from None


def sanitize_segment(value):
    text = _SEGMENT_RE.sub("_", str(value).strip())[:MAX_SEGMENT_LENGTH]
    if text in ("", ".", ".."):
        raise ExportRequestError("INVALID_OUTPUT", "Invalid S3 key path segment")
    return text


def object_keys(message):
    """S3 keys for the .xlsx and .docx artifacts."""
    output = message.get("output") or {}
    key_prefix = output.get("keyPrefix")
    if key_prefix is not None:
        if not _is_present(key_prefix):
            raise ExportRequestError("INVALID_OUTPUT", "output.keyPrefix is blank")
        raw_segments = [s for s in key_prefix.strip().split("/") if s]
        if any(s.strip() in (".", "..") for s in raw_segments):
            raise ExportRequestError(
                "INVALID_OUTPUT", "output.keyPrefix must not contain '.' or '..'"
            )
        segments = [sanitize_segment(s) for s in raw_segments]
    else:
        segments = ["exports"] + [
            sanitize_segment(message[field])
            for field in ("organizationId", "workspaceId", "tenderId", "exportId")
        ]
    prefix = "/".join(segments) + "/"
    stem = "tender-export-" + sanitize_segment(message["exportId"])
    return prefix + stem + ".xlsx", prefix + stem + ".docx"


def resolve_bucket(message):
    """Return (bucket, source): output.bucket, then S3_EXPORT_BUCKET, then S3_BUCKET."""
    bucket = (message.get("output") or {}).get("bucket")
    if bucket is not None:
        if not _is_present(bucket):
            raise ExportRequestError("INVALID_OUTPUT", "output.bucket is blank")
        return bucket.strip(), "output.bucket"
    bucket, source = default_bucket()
    if bucket is None:
        raise ExportRequestError(
            "CONFIGURATION_ERROR",
            "No export bucket: set output.bucket, "
            f"{EXPORT_BUCKET_ENV} or {FALLBACK_BUCKET_ENV}",
        )
    return bucket, source


def snapshot_sha256(message):
    canonical = json.dumps(
        message.get("snapshot"), sort_keys=True, ensure_ascii=False
    ).encode("utf-8", "surrogatepass")
    return hashlib.sha256(canonical).hexdigest()


# Ledger ----------------------------------------------------------------------

def _ledger_lock_path():
    return EXPORT_LEDGER_PATH.with_name(EXPORT_LEDGER_PATH.name + ".lock")


@contextmanager
def _ledger_lock():
    """Thread lock plus, where available, an fcntl lock for other processes
    on the same host and filesystem (not across replicas)."""
    with _LEDGER_LOCK:
        if fcntl is None:
            yield
            return
        try:
            EXPORT_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
            handle = open(_ledger_lock_path(), "a")
        except OSError as exc:
            raise LedgerUnavailableError(
                f"Export ledger lock unavailable ({type(exc).__name__})"
            ) from None
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _preserve_unreadable_ledger():
    """Copy the unreadable ledger aside; the original is never modified."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = EXPORT_LEDGER_PATH.with_name(
        f"{EXPORT_LEDGER_PATH.name}.corrupt-{stamp}"
    )
    if target.exists():
        return target
    try:
        shutil.copy2(EXPORT_LEDGER_PATH, target)
    except OSError as exc:
        print(
            f"Could not copy the unreadable export ledger ({type(exc).__name__}); "
            "the original file was left untouched."
        )
        return None
    return target


def _load_ledger():
    """The ledger dict. A missing file is an empty ledger; anything else that
    cannot be read as {exportId: entry} raises LedgerUnavailableError."""
    try:
        raw = EXPORT_LEDGER_PATH.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        reason = type(exc).__name__
    else:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            reason = type(exc).__name__
        else:
            if isinstance(data, dict) and all(
                isinstance(entry, dict) for entry in data.values()
            ):
                return data
            reason = "unexpected structure"
    copy = _preserve_unreadable_ledger()
    raise LedgerUnavailableError(
        f"Export ledger {EXPORT_LEDGER_PATH} is unreadable ({reason}); "
        "it was left untouched"
        + (f" and copied to {copy.name}" if copy else "")
        + ". Repair or move it aside, then restart the export worker."
    )


def check_ledger_available():
    with _ledger_lock():
        _load_ledger()


def find_ledger_entry(export_id):
    if not _is_present(export_id):
        return None
    with _ledger_lock():
        return _load_ledger().get(export_id)


def record_ledger_entry(export_id, entry):
    with _ledger_lock():
        ledger = _load_ledger()
        ledger.pop(export_id, None)
        ledger[export_id] = entry
        while len(ledger) > EXPORT_LEDGER_MAX:
            ledger.pop(next(iter(ledger)))
        EXPORT_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = EXPORT_LEDGER_PATH.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary_path.replace(EXPORT_LEDGER_PATH)


def _record_safely(export_id, entry):
    """Fail safely when the idempotency ledger cannot be persisted."""
    try:
        record_ledger_entry(export_id, entry)
    except OSError as exc:
        raise LedgerUnavailableError(
            f"Export ledger could not be written ({type(exc).__name__})."
        ) from exc


# Response events ---------------------------------------------------------------

def _base_event(message, event_type, status):
    event = {
        "schemaVersion": SCHEMA_VERSION,
        "eventType": event_type,
        "eventId": str(uuid4()),
    }
    for field in CORRELATION_FIELDS:
        event[field] = _echo(message.get(field))
    event["requestEventId"] = _echo(message.get("eventId"))
    attempt = message.get("attempt")
    event["attempt"] = (
        attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else None
    )
    event["status"] = status
    return event


def build_completed_event(message, snapshot, artifacts):
    event = _base_event(message, EXPORT_COMPLETED, "COMPLETED")
    sections = snapshot["draftSections"]
    approved = approved_sections(sections)
    event["completedAt"] = _now()
    event["artifacts"] = artifacts
    event["warnings"] = {
        "mandatoryWithoutDecision": count_mandatory_without_decision(snapshot),
        "approvedSectionCount": len(approved),
        "excludedSectionCount": len(sections) - len(approved),
    }
    event["counts"] = {
        "requirements": len(snapshot["requirements"]),
        "actions": len(snapshot["actions"]),
    }
    event["error"] = None
    return event


def build_failed_event(message, code, error_message, retryable):
    event = _base_event(message, EXPORT_FAILED, "FAILED")
    event["failedAt"] = _now()
    event["artifacts"] = []
    event["error"] = {
        "code": code,
        "message": error_message,
        "retryable": retryable,
    }
    return event


def _publish_failure(message, code, error_message, retryable):
    """Publish RFP_EXPORT_FAILED when an exportId allows correlation."""
    if _echo(message.get("exportId")) is None:
        print("No usable exportId in request; no failure event can be correlated.")
        return False
    try:
        publish_to_queue(
            build_failed_event(message, code, error_message, retryable),
            RESPONSE_QUEUE,
        )
    except Exception as exc:
        print(f"Could not publish {EXPORT_FAILED} ({type(exc).__name__}).")
        return False
    print(f"{EXPORT_FAILED} published (code={code}).")
    return True


# Processing --------------------------------------------------------------------

def _render_and_upload(message, snapshot, snapshot_hash):
    export_id = message["exportId"]
    previous = find_ledger_entry(export_id)
    if (
        previous
        and previous.get("status") == "UPLOADED"
        and previous.get("snapshotSha256") == snapshot_hash
    ):
        print("Artifacts already uploaded for this exportId; reusing them.")
        return previous["artifacts"]

    bucket, bucket_source = resolve_bucket(message)
    print(f"Export bucket source: {bucket_source}")
    excel_key, word_key = object_keys(message)
    try:
        excel_bytes = render_excel(snapshot)
        word_bytes = render_word(snapshot)
    except Exception as exc:
        raise ExportRequestError(
            "RENDER_FAILED", f"Export rendering failed ({type(exc).__name__})"
        ) from None

    artifacts = []
    for artifact_type, data, key, content_type in (
        ("EXCEL", excel_bytes, excel_key, XLSX_CONTENT_TYPE),
        ("WORD", word_bytes, word_key, DOCX_CONTENT_TYPE),
    ):
        try:
            uploaded = upload_bytes(data, bucket, key, content_type)
        except Exception as exc:
            raise TransientExportError(
                "UPLOAD_FAILED", f"S3 upload failed ({type(exc).__name__})"
            ) from None
        artifacts.append({"type": artifact_type, **uploaded})
        print(f"Uploaded {artifact_type} artifact: {uploaded['sizeBytes']} bytes")

    _record_safely(export_id, {
        "status": "UPLOADED",
        "snapshotSha256": snapshot_hash,
        "artifacts": artifacts,
        "updatedAt": _now(),
    })
    return artifacts


def process_export_delivery(body, redelivered):
    """Process one export request and return how it must be settled.

    Never touches the channel, so it is safe to run off the connection thread.
    """
    message = {}
    try:
        message = parse_body(body)
        print("\nExport request received:")
        print("eventType:", message.get("eventType"))
        print("exportId:", _echo(message.get("exportId")))
        print("redelivered:", bool(redelivered))

        export_id = message.get("exportId")
        entry = find_ledger_entry(export_id)
        if entry is not None and entry.get("status") == "COMPLETED":
            if entry.get("snapshotSha256") == snapshot_sha256(message):
                print(
                    "Duplicate export delivery: completion already published "
                    f"(responseEventId={entry.get('responseEventId')}). "
                    "Acknowledging without re-uploading or republishing."
                )
                return ACK
            raise ExportRequestError(
                "EXPORT_ID_CONFLICT",
                "exportId was already completed with a different snapshot",
            )

        snapshot = validate_request(message)
        snapshot_hash = snapshot_sha256(message)
        artifacts = _render_and_upload(message, snapshot, snapshot_hash)

        completed = build_completed_event(message, snapshot, artifacts)
        try:
            publish_to_queue(completed, RESPONSE_QUEUE)
        except Exception as exc:
            raise TransientExportError(
                "PUBLISH_FAILED",
                f"Completion publish failed ({type(exc).__name__})",
            ) from None
        print(f"{EXPORT_COMPLETED} published for exportId={export_id}.")
        try:
            _record_safely(export_id, {
                "status": "COMPLETED",
                "snapshotSha256": snapshot_hash,
                "artifacts": artifacts,
                "responseEventId": completed["eventId"],
                "updatedAt": _now(),
            })
        except LedgerUnavailableError as exc:
            print(
                f"{exc} The completion was published, so the request is "
                "acknowledged, but the export worker will stop."
            )
            return ACK_AND_STOP
        return ACK

    except LedgerUnavailableError as exc:
        print(
            f"{exc} The request is requeued unprocessed (no event published) "
            "and the export worker will stop."
        )
        return REQUEUE_AND_STOP

    except ExportRequestError as exc:
        print(f"Export request rejected ({exc.code}): {exc}")
        published = _publish_failure(message, exc.code, str(exc), False)
        if published or _echo(message.get("exportId")) is None or redelivered:
            return REJECT
        print("Failure event not published; requeuing once to retry it.")
        return REQUEUE

    except TransientExportError as exc:
        print(f"Export failed ({exc.code}): {exc}")
        if not redelivered:
            print("Requeuing export request for one retry.")
            return REQUEUE
        _publish_failure(message, exc.code, str(exc), True)
        print("Export request was already redelivered. Rejecting without requeue.")
        return REJECT

    except Exception as exc:
        print(f"Unexpected export failure ({type(exc).__name__}).")
        if not redelivered:
            return REQUEUE
        _publish_failure(
            message, "INTERNAL_ERROR",
            f"Unexpected export failure ({type(exc).__name__})", True,
        )
        return REJECT


# Channel handling (mirrors app.messaging.consumer) ----------------------------

def settle_delivery(ch, delivery_tag, outcome):
    """Ack/nack on the connection thread; never raises on a dead channel.

    For a *_AND_STOP outcome, consuming is stopped after settlement.
    """
    stop = outcome in _STOP_OUTCOMES
    action = _STOP_OUTCOMES.get(outcome, outcome)
    if not ch.is_open:
        print(
            "RabbitMQ channel is closed. No acknowledgement or rejection was "
            "attempted; the broker will redeliver the export request."
        )
        return False
    settled = True
    try:
        if action == ACK:
            ch.basic_ack(delivery_tag=delivery_tag)
            print("Export request acknowledged.")
        else:
            ch.basic_nack(delivery_tag=delivery_tag, requeue=action == REQUEUE)
    except AMQPError as exc:
        print(
            f"Could not {action} the export request ({type(exc).__name__}). "
            "The broker will redeliver it."
        )
        settled = False
    if stop:
        print("Stopping the export worker: the export ledger is unavailable.")
        try:
            ch.stop_consuming()
        except AMQPError:
            pass
    return settled


def callback(ch, method, properties, body):
    """Synchronous processing and settlement (used directly by tests)."""
    outcome = process_export_delivery(body, method.redelivered)
    settle_delivery(ch, method.delivery_tag, outcome)


def _start_delivery_worker(connection, ch, method, body, state):
    def run():
        started = time.perf_counter()
        outcome = process_export_delivery(body, method.redelivered)
        print(
            f"Export request finished in {time.perf_counter() - started:.1f}s; "
            f"outcome={outcome}",
            flush=True,
        )
        if outcome in _STOP_OUTCOMES:
            state["fatal"] = True
        elif outcome == REQUEUE:
            # Off the connection thread, so heartbeats continue meanwhile.
            delay = requeue_delay_seconds()
            if delay > 0:
                print(f"Waiting {delay:g}s before requeuing.", flush=True)
                time.sleep(delay)
        try:
            connection.add_callback_threadsafe(
                functools.partial(settle_delivery, ch, method.delivery_tag, outcome)
            )
        except AMQPError as exc:
            print(
                "RabbitMQ connection closed before settlement "
                f"({type(exc).__name__}); the broker will redeliver the request "
                "and the export ledger prevents a duplicate publish."
            )

    worker = threading.Thread(target=run, name="rfp-export-delivery", daemon=True)
    state["worker"] = worker
    worker.start()


def _wait_for_worker(state):
    worker = state.get("worker")
    if worker is not None and worker.is_alive():
        print("Waiting for the in-flight export to finish before reconnecting...")
        worker.join()
    state["worker"] = None


def _consume(state):
    connection = create_connection()
    state["connected"] = True
    try:
        channel = connection.channel()
        channel.queue_declare(queue=REQUEST_QUEUE, durable=True)
        channel.basic_qos(prefetch_count=1)
        channel.basic_consume(
            queue=REQUEST_QUEUE,
            on_message_callback=lambda ch, method, properties, body: (
                _start_delivery_worker(connection, ch, method, body, state)
            ),
            auto_ack=False,
        )
        print(f"Connected. Waiting for export requests from '{REQUEST_QUEUE}'...")
        print(f"Export responses go to '{RESPONSE_QUEUE}'.")
        print("Press Ctrl+C to stop the export worker.")
        channel.start_consuming()
    finally:
        if connection.is_open:
            try:
                connection.close()
            except AMQPError:
                pass


def _preflight():
    """Checks run before any broker connection; returns an exit code or None."""
    try:
        validate_queue_configuration(
            REQUEST_QUEUE, RESPONSE_QUEUE, _configured_analysis_request_queue()
        )
    except ExportConfigurationError as exc:
        print(f"Export worker not started (configuration error): {exc}", flush=True)
        return EXIT_CONFIGURATION_ERROR
    try:
        check_ledger_available()
    except LedgerUnavailableError as exc:
        print(f"Export worker not started: {exc}", flush=True)
        return EXIT_LEDGER_UNAVAILABLE
    _, source = default_bucket()
    print(
        "Default export bucket source: "
        + (source or "none (requests must set output.bucket)"),
        flush=True,
    )
    return None


def _ledger_stop_exit_code():
    print(
        "Export worker stopped because the export ledger is unavailable. "
        "Repair it, then restart the worker.",
        flush=True,
    )
    return EXIT_LEDGER_UNAVAILABLE


def start_export_consumer():
    """Run the worker; returns None on a normal stop, else an exit code."""
    exit_code = _preflight()
    if exit_code is not None:
        return exit_code
    state = {"worker": None}
    delay = RECONNECT_DELAY_SECONDS
    while True:
        try:
            _consume(state)
            if state.get("fatal"):
                return _ledger_stop_exit_code()
            return None
        except KeyboardInterrupt:
            print("\nExport worker stopped by user.")
            worker = state.get("worker")
            if worker is not None and worker.is_alive():
                print(
                    "An in-flight export was not acknowledged; "
                    "the broker will redeliver it."
                )
            return None
        except (AMQPConnectionError, AMQPChannelError) as exc:
            if state.pop("connected", False):
                delay = RECONNECT_DELAY_SECONDS
            print(f"\nRabbitMQ connection lost: {type(exc).__name__}: {exc}")
            _wait_for_worker(state)
            if state.get("fatal"):
                return _ledger_stop_exit_code()
            print(f"Reconnecting in {delay}s...")
            time.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX_SECONDS)


if __name__ == "__main__":
    sys.exit(start_export_consumer())
