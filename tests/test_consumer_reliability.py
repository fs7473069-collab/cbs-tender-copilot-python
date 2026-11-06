import json
import threading
from unittest.mock import MagicMock, Mock, patch

import pika
import pytest
from pika.exceptions import (
    ChannelWrongStateError,
    ConnectionWrongStateError,
    NackError,
    StreamLostError,
)

import app.messaging.consumer as consumer
import app.messaging.publisher as publisher


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(consumer, "DEBUG_RESPONSE_PATH", tmp_path / "actual_response.json")
    monkeypatch.setattr(consumer, "COMPLETION_LEDGER_PATH", tmp_path / "ledger.json")
    monkeypatch.setattr(consumer, "DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setattr(
        consumer, "summarize_tender_document", lambda *args, **kwargs: {}
    )


@pytest.fixture
def pipeline(monkeypatch):
    mocks = {
        "download": Mock(),
        "process": Mock(return_value=[{
            "text": "Requirement",
            "evidence_match": {"status": "NOT_FOUND", "evidence": [], "note": "n"},
        }]),
        "publish": Mock(),
    }
    monkeypatch.setattr(consumer, "download_file", mocks["download"])
    monkeypatch.setattr(consumer, "process_tender_document", mocks["process"])
    monkeypatch.setattr(consumer, "publish_message", mocks["publish"])
    return mocks


def make_message(event_id="event-1", job_id="job-123"):
    return {
        "organizationId": "org",
        "workspaceId": "ws",
        "jobId": job_id,
        "analysisId": "analysis-1",
        "correlationId": "corr-1",
        "eventId": event_id,
        "documents": [{
            "documentId": "doc-1",
            "filename": "tender.docx",
            "bucket": "bucket",
            "key": "tender.docx",
        }],
    }


def body(message):
    return json.dumps(message).encode("utf-8")


def make_channel():
    channel = Mock()
    channel.is_open = True
    return channel


def make_method(tag, redelivered=False):
    method = Mock()
    method.delivery_tag = tag
    method.redelivered = redelivered
    return method


# Publisher confirmation -------------------------------------------------------

def _publisher_channel(monkeypatch):
    connection = MagicMock()
    connection.is_open = True
    channel = connection.channel.return_value
    monkeypatch.setattr(publisher, "create_connection", lambda: connection)
    return connection, channel


def test_publisher_enables_confirms_before_persistent_mandatory_publish(monkeypatch):
    connection, channel = _publisher_channel(monkeypatch)
    order = []
    channel.confirm_delivery.side_effect = lambda: order.append("confirm")
    channel.basic_publish.side_effect = lambda **kwargs: order.append("publish")

    publisher.publish_message({"result": {"requirements": [{}]}})

    assert order == ["confirm", "publish"]
    kwargs = channel.basic_publish.call_args.kwargs
    assert kwargs["mandatory"] is True
    assert kwargs["properties"].delivery_mode == 2
    connection.close.assert_called_once()


def test_publisher_nack_propagates_so_request_is_not_acknowledged(monkeypatch, pipeline):
    connection, channel = _publisher_channel(monkeypatch)
    channel.basic_publish.side_effect = NackError([])
    monkeypatch.setattr(consumer, "publish_message", publisher.publish_message)
    ch = make_channel()

    consumer.callback(ch, make_method(1), Mock(), body(make_message()))

    ch.basic_ack.assert_not_called()
    ch.basic_nack.assert_called_once_with(delivery_tag=1, requeue=True)
    assert not consumer.COMPLETION_LEDGER_PATH.exists()
    connection.close.assert_called_once()


# Settlement on closed / failing channels -------------------------------------

def test_closed_channel_after_publish_does_not_ack_or_raise(pipeline):
    ch = make_channel()
    ch.is_open = False

    consumer.callback(ch, make_method(2), Mock(), body(make_message()))

    pipeline["publish"].assert_called_once()
    ch.basic_ack.assert_not_called()
    ch.basic_nack.assert_not_called()
    # Completion is durable, so the inevitable redelivery is not republished.
    assert consumer._find_completed_delivery(consumer._delivery_key(make_message()))


@pytest.mark.parametrize("error", [
    StreamLostError("Stream connection lost: BrokenPipeError(32, 'Broken pipe')"),
    ChannelWrongStateError("Channel is closed."),
    ConnectionWrongStateError(),
])
def test_ack_failure_is_logged_not_raised(pipeline, error, capsys):
    ch = make_channel()
    ch.basic_ack.side_effect = error

    consumer.callback(ch, make_method(3), Mock(), body(make_message()))

    ch.basic_ack.assert_called_once_with(delivery_tag=3)
    ch.basic_nack.assert_not_called()
    assert "The broker will redeliver it." in capsys.readouterr().out


def test_nack_failure_is_logged_not_raised(pipeline):
    pipeline["download"].side_effect = RuntimeError("S3 down")
    ch = make_channel()
    ch.basic_nack.side_effect = StreamLostError("lost")

    assert consumer.process_delivery(body(make_message()), False) == consumer.REQUEUE
    assert consumer.settle_delivery(ch, 4, consumer.REQUEUE) is False


# Redelivery and duplicate events ---------------------------------------------

def test_redelivery_of_completed_request_is_acked_without_republishing(pipeline, capsys):
    first = make_channel()
    first.basic_ack.side_effect = StreamLostError("Broken pipe")
    consumer.callback(first, make_method(5), Mock(), body(make_message()))
    assert pipeline["publish"].call_count == 1

    redelivery = make_channel()
    consumer.callback(redelivery, make_method(6, redelivered=True), Mock(), body(make_message()))

    assert pipeline["publish"].call_count == 1
    assert pipeline["process"].call_count == 1
    assert pipeline["download"].call_count == 1
    redelivery.basic_ack.assert_called_once_with(delivery_tag=6)
    assert "Duplicate delivery" in capsys.readouterr().out


def test_duplicate_event_id_is_not_published_twice(pipeline):
    for tag in (7, 8):
        consumer.callback(make_channel(), make_method(tag), Mock(), body(make_message()))
    assert pipeline["publish"].call_count == 1


def test_new_event_for_same_job_is_processed(pipeline):
    consumer.callback(make_channel(), make_method(9), Mock(), body(make_message("event-1")))
    consumer.callback(make_channel(), make_method(10), Mock(), body(make_message("event-2")))
    assert pipeline["publish"].call_count == 2


def test_failed_publish_is_retried_on_redelivery(pipeline):
    pipeline["publish"].side_effect = [RuntimeError("broker down"), None]
    first = make_channel()
    consumer.callback(first, make_method(11), Mock(), body(make_message()))
    first.basic_nack.assert_called_once_with(delivery_tag=11, requeue=True)

    second = make_channel()
    consumer.callback(second, make_method(12, redelivered=True), Mock(), body(make_message()))

    assert pipeline["publish"].call_count == 2
    second.basic_ack.assert_called_once_with(delivery_tag=12)


def test_redelivered_failure_is_rejected_not_acknowledged(pipeline):
    pipeline["process"].side_effect = RuntimeError("extraction failed")
    ch = make_channel()

    consumer.callback(ch, make_method(13, redelivered=True), Mock(), body(make_message()))

    ch.basic_ack.assert_not_called()
    ch.basic_nack.assert_called_once_with(delivery_tag=13, requeue=False)
    pipeline["publish"].assert_not_called()


def test_message_without_job_id_is_neither_acknowledged_nor_recorded(pipeline):
    message = make_message()
    del message["jobId"]
    ch = make_channel()

    consumer.callback(ch, make_method(14), Mock(), body(message))

    # Missing jobId fails status-event validation; never acknowledged.
    ch.basic_ack.assert_not_called()
    assert not consumer.COMPLETION_LEDGER_PATH.exists()


def test_ledger_write_failure_still_acks_published_response(pipeline, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(consumer, "_record_completed_delivery", fail)
    ch = make_channel()

    consumer.callback(ch, make_method(15), Mock(), body(make_message()))

    ch.basic_ack.assert_called_once_with(delivery_tag=15)
    assert "Completion ledger could not be written" in capsys.readouterr().out


# Worker thread / connection loss --------------------------------------------

def test_worker_runs_off_io_thread_and_settles_via_threadsafe_callback(pipeline):
    scheduled = []
    seen_threads = []
    pipeline["process"].side_effect = lambda path: (
        seen_threads.append(threading.current_thread()) or [{"text": "R"}]
    )
    connection = Mock()
    connection.add_callback_threadsafe.side_effect = scheduled.append
    ch = make_channel()
    state = {"worker": None}

    consumer._start_delivery_worker(connection, ch, make_method(16), body(make_message()), state)
    state["worker"].join(timeout=5)

    assert seen_threads and seen_threads[0] is not threading.main_thread()
    ch.basic_ack.assert_not_called()
    assert len(scheduled) == 1
    scheduled[0]()
    ch.basic_ack.assert_called_once_with(delivery_tag=16)


def test_connection_loss_before_settlement_is_handled(pipeline, capsys):
    connection = Mock()
    connection.add_callback_threadsafe.side_effect = ConnectionWrongStateError()
    state = {"worker": None}

    consumer._start_delivery_worker(connection, make_channel(), make_method(17), body(make_message()), state)
    state["worker"].join(timeout=5)

    assert "connection closed before settlement" in capsys.readouterr().out
    assert consumer._find_completed_delivery(consumer._delivery_key(make_message()))


def test_start_consumer_reconnects_after_stream_loss_and_waits_for_worker(monkeypatch):
    calls = []
    finished = threading.Event()
    worker = threading.Thread(target=lambda: finished.wait(timeout=5))
    worker.start()

    def fake_consume(state):
        calls.append(len(calls))
        state["connected"] = True
        if len(calls) == 1:
            state["worker"] = worker
            finished.set()
            raise StreamLostError("Broken pipe")

    sleeps = []
    monkeypatch.setattr(consumer, "_consume", fake_consume)
    monkeypatch.setattr(consumer, "_log_knowledge_base_status", lambda: None)
    monkeypatch.setattr(consumer.time, "sleep", sleeps.append)

    consumer.start_consumer()

    assert calls == [0, 1]
    assert not worker.is_alive()
    assert sleeps == [consumer.RECONNECT_DELAY_SECONDS]


def test_start_consumer_does_not_loop_on_configuration_error(monkeypatch):
    monkeypatch.setattr(consumer, "_log_knowledge_base_status", lambda: None)
    monkeypatch.setattr(
        consumer, "_consume",
        Mock(side_effect=ValueError("Missing RabbitMQ configuration.")),
    )
    with pytest.raises(ValueError, match="Missing RabbitMQ configuration"):
        consumer.start_consumer()


def test_consume_uses_prefetch_one_and_manual_ack(monkeypatch):
    connection = MagicMock()
    connection.is_open = True
    channel = connection.channel.return_value
    monkeypatch.setattr(consumer, "create_connection", lambda: connection)

    consumer._consume({"worker": None})

    channel.basic_qos.assert_called_once_with(prefetch_count=1)
    assert channel.basic_consume.call_args.kwargs["auto_ack"] is False
    channel.start_consuming.assert_called_once()
    connection.close.assert_called_once()
