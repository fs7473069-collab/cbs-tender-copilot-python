import json
from unittest.mock import MagicMock, patch

from app.messaging.publisher import publish_message


@patch("app.messaging.publisher.create_connection")
def test_publish_message_sends_json_and_confirms_delivery(mock_create_connection):
    # Create a fake RabbitMQ connection.
    connection = MagicMock()
    channel = MagicMock()

    mock_create_connection.return_value = connection
    connection.channel.return_value = channel
    connection.is_open = True

    # Sample message; this will NOT be sent to real RabbitMQ.
    message = {
        "eventType": "RFP_ANALYSIS_COMPLETED",
        "status": "COMPLETED",
        "result": {"requirements": []},
    }

    publish_message(message)

    # Verify publisher setup.
    channel.queue_declare.assert_called_once_with(
        queue="rfp_docs_response1",
        durable=True,
    )
    channel.confirm_delivery.assert_called_once()

    # Verify message and publishing settings.
    args, kwargs = channel.basic_publish.call_args

    assert kwargs["exchange"] == ""
    assert kwargs["routing_key"] == "rfp_docs_response1"
    assert kwargs["mandatory"] is True
    assert json.loads(kwargs["body"]) == message
    assert kwargs["properties"].content_type == "application/json"
    assert kwargs["properties"].delivery_mode == 2

    # Verify connection is closed.
    connection.close.assert_called_once()
from unittest.mock import MagicMock, patch

import pika
import pytest

from app.messaging.publisher import publish_message


@patch("app.messaging.publisher.create_connection")
def test_publish_message_raises_when_broker_nacks(mock_create_connection):
    connection = MagicMock()
    channel = MagicMock()

    mock_create_connection.return_value = connection
    connection.channel.return_value = channel
    connection.is_open = True

    channel.basic_publish.side_effect = pika.exceptions.NackError([])

    with pytest.raises(pika.exceptions.NackError):
        publish_message({"eventType": "RFP_ANALYSIS_COMPLETED"})

    connection.close.assert_called_once()


@patch("app.messaging.publisher.create_connection")
def test_publish_message_raises_when_message_is_unroutable(mock_create_connection):
    connection = MagicMock()
    channel = MagicMock()

    mock_create_connection.return_value = connection
    connection.channel.return_value = channel
    connection.is_open = True

    channel.basic_publish.side_effect = pika.exceptions.UnroutableError([])

    with pytest.raises(pika.exceptions.UnroutableError):
        publish_message({"eventType": "RFP_ANALYSIS_COMPLETED"})

    connection.close.assert_called_once()


@patch("app.messaging.publisher.create_connection")
def test_connection_closes_when_publish_fails(mock_create_connection):
    connection = MagicMock()
    channel = MagicMock()

    mock_create_connection.return_value = connection
    connection.channel.return_value = channel
    connection.is_open = True

    channel.basic_publish.side_effect = RuntimeError("Simulated publish failure")

    with pytest.raises(RuntimeError, match="Simulated publish failure"):
        publish_message({"eventType": "RFP_ANALYSIS_COMPLETED"})

    connection.close.assert_called_once()
