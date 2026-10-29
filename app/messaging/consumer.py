import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from pika.exceptions import AMQPError

from app.messaging.rabbitmq import create_connection
from app.storage.s3_client import download_file
from app.orchestration.service import process_tender_document
from app.response.response_builder import build_completed_response
from app.messaging.publisher import publish_message


load_dotenv()

QUEUE = os.getenv("RABBITMQ_QUEUE", "rfp-python-test")
DOWNLOAD_DIR = os.path.join("data", "s3_test")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEBUG_RESPONSE_PATH = (
    PROJECT_ROOT / "local_data" / "analysis" / "actual_response.json"
)


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


def callback(ch, method, properties, body):
    try:
        message = _log_inbound_body(body)

        print("\nRFP message received:")
        print("eventType:", message.get("eventType"))
        print("jobId:", message.get("jobId"))
        print("eventId:", message.get("eventId"))

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
        with_evidence = sum(
            1
            for requirement in requirements
            if isinstance(requirement, dict)
            and "evidence_match" in requirement
        )
        print(f"Requirements containing RAG evidence: {with_evidence}")

        response = build_completed_response(
            request_message=message,
            requirements=requirements,
        )
        response_requirements = (
            (response.get("result") or {}).get("requirements") or []
        )
        if len(response_requirements) != len(requirements):
            raise ValueError(
                "Completed response requirement count does not match "
                "fresh extraction results."
            )

        # Debug artifact only; never used as input for publishing.
        _save_debug_response(response)

        print(f"\nResponse saved to: {DEBUG_RESPONSE_PATH.resolve()}")
        print(
            "Saved response size:",
            DEBUG_RESPONSE_PATH.stat().st_size,
            "bytes",
        )
        print("Event type:", response.get("eventType"))
        print("Job ID:", response.get("jobId"))
        print("Requirement count:", len(response_requirements))

        # Validate request IDs for the companion status event before publish.
        status_event = _build_completed_status_event(
            message=message,
            document=document,
        )

        print("\nPublishing completion response to the response queue...")
        # Publish the fresh RFP_ANALYSIS_COMPLETED payload (with requirements),
        # not a status-only shell and not a previously saved analysis file.
        publish_message(response)
        print("Completion response published successfully.")
        print(
            "Status companion event prepared "
            f"(not published): {status_event['eventType']}"
        )

        # Acknowledge only after publishing succeeds.
        if ch.is_open:
            ch.basic_ack(delivery_tag=method.delivery_tag)
            print("Request acknowledged successfully.")
        else:
            print(
                "RabbitMQ channel closed before acknowledgement. "
                "The request may be redelivered."
            )

    except Exception as exc:
        print(f"\nRequest processing failed: {exc}")

        if not ch.is_open:
            print(
                "RabbitMQ channel is closed. "
                "No acknowledgement or rejection was attempted."
            )
            return

        try:
            if method.redelivered:
                print(
                    "Request was already redelivered. "
                    "Rejecting without requeue."
                )
                ch.basic_nack(
                    delivery_tag=method.delivery_tag,
                    requeue=False,
                )
            else:
                print("Requeuing request for one retry.")
                ch.basic_nack(
                    delivery_tag=method.delivery_tag,
                    requeue=True,
                )

        except AMQPError as nack_error:
            print(
                "Could not reject/requeue the request because "
                f"RabbitMQ reported an error: {nack_error}"
            )


def start_consumer():
    connection = create_connection()

    try:
        channel = connection.channel()
        channel.basic_qos(prefetch_count=1)

        channel.basic_consume(
            queue=QUEUE,
            on_message_callback=callback,
            auto_ack=False,
        )

        print(f"Connected. Waiting for messages from '{QUEUE}'...")
        print("Press Ctrl+C to stop the consumer.")

        channel.start_consuming()

    except KeyboardInterrupt:
        print("\nConsumer stopped by user.")

    finally:
        if connection.is_open:
            connection.close()


if __name__ == "__main__":
    start_consumer()
