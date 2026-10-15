
import json
from unittest.mock import Mock, patch

from app.messaging.consumer import callback


def make_message():
    return {
        "organizationId": "constell-dev",
        "documents": [
            {
                "documentId": "document-123",
                "filename": "tender.docx",
                "bucket": "test-bucket",
                "key": "tender.docx",
            }
        ],
    }


def make_channel():
    channel = Mock()
    channel.is_open = True
    return channel


@patch("app.messaging.consumer.publish_message")
@patch("app.messaging.consumer.process_document")
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
    channel.basic_ack.assert_called_once_with(delivery_tag=10)
    channel.basic_nack.assert_not_called()


@patch("app.messaging.consumer.publish_message")
@patch("app.messaging.consumer.process_document")
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
