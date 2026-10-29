
import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

import app.messaging.consumer as consumer
from app.messaging.consumer import _log_inbound_body, callback


@pytest.fixture(autouse=True)
def debug_response_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        consumer,
        "DEBUG_RESPONSE_PATH",
        tmp_path / "actual_response.json",
    )


def test_log_inbound_body_reports_bytes_and_requirement_count(capsys):
    payload = {
        "eventType": "RFP_ANALYSIS_COMPLETED",
        "jobId": "job-1",
        "eventId": "event-1",
        "result": {"requirements": [{}, {}, {}]},
    }
    body = json.dumps(payload).encode("utf-8")

    message = _log_inbound_body(body)
    captured = capsys.readouterr().out

    assert message["jobId"] == "job-1"
    assert "Raw received body length (bytes): %s" % len(body) in captured
    assert "result.requirements count: 3" in captured
    assert "The same designs are available" not in captured
    assert json.dumps(payload) not in captured


def make_message():
    return {
        "organizationId": "constell-dev",
        "workspaceId": "workspace-123",
        "jobId": "job-123",
        "analysisId": "analysis-123",
        "correlationId": "correlation-123",
        "documents": [
            {
                "documentId": "document-123",
                "filename": "tender.docx",
                "bucket": "test-bucket",
                "key": "tender.docx",
            },
            {
                "documentId": "document-456",
                "filename": "unprocessed.docx",
                "bucket": "test-bucket",
                "key": "unprocessed.docx",
            },
        ],
    }


def make_channel():
    channel = Mock()
    channel.is_open = True
    return channel


@patch("app.messaging.consumer.publish_message")
@patch("app.messaging.consumer.process_tender_document")
@patch("app.messaging.consumer.download_file")
def test_successful_processing_acknowledges_request(
    mock_download, mock_process, mock_publish
):
    channel = make_channel()
    method = Mock()
    method.delivery_tag = 10
    method.redelivered = False

    mock_process.return_value = [
        {
            "text": "Three-year warranty required",
            "mandatory": True,
            "confidence": "High",
            "page": 2,
        }
    ]

    callback(
        channel,
        method,
        Mock(),
        json.dumps(make_message()).encode("utf-8"),
    )

    mock_download.assert_called_once()
    mock_process.assert_called_once()
    mock_publish.assert_called_once()
    completed = mock_publish.call_args.args[0]

    assert completed["schemaVersion"] == "1.0"
    assert completed["eventType"] == "RFP_ANALYSIS_COMPLETED"
    assert completed["organizationId"] == "constell-dev"
    assert completed["workspaceId"] == "workspace-123"
    assert completed["jobId"] == "job-123"
    assert completed["analysisId"] == "analysis-123"
    assert completed["correlationId"] == "correlation-123"
    assert completed["attempt"] == 1
    assert completed["status"] == "COMPLETED"
    assert completed["processedDocumentIds"] == ["document-123", "document-456"]
    assert completed["result"]["requirements"][0]["content"] == (
        "Three-year warranty required"
    )
    assert completed["result"]["requirements"][0]["mandatory"] is True
    assert completed["eventId"]
    datetime.fromisoformat(completed["completedAt"].replace("Z", "+00:00"))
    # Debug artifact mirrors the published fresh response, not a prior run.
    saved = json.loads(consumer.DEBUG_RESPONSE_PATH.read_text(encoding="utf-8"))
    assert saved["eventType"] == "RFP_ANALYSIS_COMPLETED"
    assert len(saved["result"]["requirements"]) == 1
    assert saved["result"]["requirements"][0]["content"] == (
        "Three-year warranty required"
    )
    channel.basic_ack.assert_called_once_with(delivery_tag=10)
    channel.basic_nack.assert_not_called()


@patch("app.messaging.consumer.publish_message")
@patch("app.messaging.consumer.process_tender_document")
@patch("app.messaging.consumer.download_file")
def test_completed_response_uses_inbound_attempt(
    mock_download, mock_process, mock_publish
):
    channel = make_channel()
    method = Mock()
    method.delivery_tag = 16
    method.redelivered = False
    message = make_message()
    message["attempt"] = 3
    mock_process.return_value = [{"text": "Track vehicles", "mandatory": False}]

    callback(
        channel,
        method,
        Mock(),
        json.dumps(message).encode("utf-8"),
    )

    published = mock_publish.call_args.args[0]
    assert published["attempt"] == 3
    assert published["eventType"] == "RFP_ANALYSIS_COMPLETED"
    channel.basic_ack.assert_called_once_with(delivery_tag=16)


@patch("app.messaging.consumer.publish_message")
@patch("app.messaging.consumer.process_tender_document")
@patch("app.messaging.consumer.download_file")
def test_published_response_uses_fresh_extraction_not_stale_debug_file(
    mock_download, mock_process, mock_publish
):
    channel = make_channel()
    method = Mock()
    method.delivery_tag = 17
    method.redelivered = False

    # Pretend an older debug artifact exists with a different requirement set.
    consumer.DEBUG_RESPONSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    consumer.DEBUG_RESPONSE_PATH.write_text(
        json.dumps(
            {
                "eventType": "RFP_ANALYSIS_COMPLETED",
                "result": {
                    "requirements": [
                        {"content": f"stale-{index}"} for index in range(106)
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    mock_process.return_value = [
        {
            "text": "Fresh requirement from current extraction",
            "mandatory": True,
            "confidence": "High",
            "evidence_match": {
                "status": "REVIEW_REQUIRED",
                "evidence": [{"text": "CBS evidence", "source": "kb.pdf", "page": 1}],
                "note": "Review required.",
            },
        }
    ]

    callback(
        channel,
        method,
        Mock(),
        json.dumps(make_message()).encode("utf-8"),
    )

    published = mock_publish.call_args.args[0]
    assert published["eventType"] == "RFP_ANALYSIS_COMPLETED"
    assert len(published["result"]["requirements"]) == 1
    assert published["result"]["requirements"][0]["content"] == (
        "Fresh requirement from current extraction"
    )
    assert published["result"]["requirements"][0]["evidenceMatch"]["status"] == (
        "REVIEW_REQUIRED"
    )
    saved = json.loads(consumer.DEBUG_RESPONSE_PATH.read_text(encoding="utf-8"))
    assert len(saved["result"]["requirements"]) == 1
    assert "stale-0" not in json.dumps(saved)


@patch("app.messaging.consumer.publish_message")
@patch("app.messaging.consumer.process_tender_document")
@patch("app.messaging.consumer.download_file")
def test_missing_status_ids_requeues_without_publishing_or_acknowledging(
    mock_download, mock_process, mock_publish
):
    channel = make_channel()
    method = Mock()
    method.delivery_tag = 14
    method.redelivered = False
    message = make_message()
    del message["analysisId"]
    mock_process.return_value = [{"text": "Requirement"}]

    callback(
        channel,
        method,
        Mock(),
        json.dumps(message).encode("utf-8"),
    )

    mock_download.assert_called_once()
    mock_process.assert_called_once()
    mock_publish.assert_not_called()
    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_called_once_with(
        delivery_tag=14,
        requeue=True,
    )


@patch("app.messaging.consumer.publish_message")
@patch("app.messaging.consumer.process_tender_document")
@patch("app.messaging.consumer.download_file")
def test_publish_failure_does_not_acknowledge_request(
    mock_download, mock_process, mock_publish
):
    channel = make_channel()
    method = Mock()
    method.delivery_tag = 15
    method.redelivered = False
    mock_process.return_value = [{"text": "Requirement"}]
    mock_publish.side_effect = RuntimeError("Simulated publish failure")

    callback(
        channel,
        method,
        Mock(),
        json.dumps(make_message()).encode("utf-8"),
    )

    mock_publish.assert_called_once()
    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_called_once_with(
        delivery_tag=15,
        requeue=True,
    )


@patch("app.messaging.consumer.publish_message")
@patch("app.messaging.consumer.process_tender_document")
@patch("app.messaging.consumer.download_file")
def test_first_failure_requeues_request(
    mock_download, mock_process, mock_publish
):
    channel = make_channel()
    method = Mock()
    method.delivery_tag = 11
    method.redelivered = False
    mock_download.side_effect = RuntimeError("Simulated S3 failure")

    callback(
        channel,
        method,
        Mock(),
        json.dumps(make_message()).encode("utf-8"),
    )

    channel.basic_nack.assert_called_once_with(
        delivery_tag=11,
        requeue=True,
    )
    channel.basic_ack.assert_not_called()
    mock_publish.assert_not_called()


@patch("app.messaging.consumer.publish_message")
@patch("app.messaging.consumer.download_file")
def test_redelivered_failure_is_not_requeued(
    mock_download, mock_publish
):
    channel = make_channel()
    method = Mock()
    method.delivery_tag = 12
    method.redelivered = True
    mock_download.side_effect = RuntimeError("Simulated S3 failure")

    callback(
        channel,
        method,
        Mock(),
        json.dumps(make_message()).encode("utf-8"),
    )

    channel.basic_nack.assert_called_once_with(
        delivery_tag=12,
        requeue=False,
    )
    channel.basic_ack.assert_not_called()
    mock_publish.assert_not_called()


def test_closed_channel_does_not_acknowledge_or_reject():
    channel = make_channel()
    channel.is_open = False

    method = Mock()
    method.delivery_tag = 13
    method.redelivered = False

    with patch(
        "app.messaging.consumer.download_file",
        side_effect=RuntimeError("Simulated failure"),
    ):
        callback(
            channel,
            method,
            Mock(),
            json.dumps(make_message()).encode("utf-8"),
        )

    channel.basic_ack.assert_not_called()
    channel.basic_nack.assert_not_called()

