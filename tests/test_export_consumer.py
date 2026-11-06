"""Export worker tests. All S3 and RabbitMQ access is mocked."""

import json
import threading
from io import BytesIO
from unittest.mock import MagicMock, Mock

import pytest
from openpyxl import load_workbook

import app.messaging.consumer as analysis_consumer
import app.messaging.export_consumer as export_consumer
import app.messaging.publisher as publisher
import app.storage.s3_client as s3_client
from tests.export_fixtures import make_export_request


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(export_consumer, "EXPORT_LEDGER_PATH", tmp_path / "exports.json")
    monkeypatch.delenv("S3_EXPORT_BUCKET", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setenv("EXPORT_REQUEUE_DELAY_SECONDS", "0")

    def no_real_connection():
        raise AssertionError("Tests must not open a real RabbitMQ connection")

    monkeypatch.setattr(publisher, "create_connection", no_real_connection)
    monkeypatch.setattr(export_consumer, "create_connection", no_real_connection)
    monkeypatch.setattr(
        s3_client, "create_s3_client",
        Mock(side_effect=AssertionError("Tests must not create a real S3 client")),
    )


@pytest.fixture
def io_mocks(monkeypatch):
    uploads = []

    def fake_upload(data, bucket, key, content_type):
        uploads.append({"data": data, "bucket": bucket, "key": key,
                        "contentType": content_type})
        return {"bucket": bucket, "key": key, "contentType": content_type,
                "sizeBytes": len(data), "sha256": "test-sha"}

    mocks = {"upload": Mock(side_effect=fake_upload), "publish": Mock(),
             "uploads": uploads}
    monkeypatch.setattr(export_consumer, "upload_bytes", mocks["upload"])
    monkeypatch.setattr(export_consumer, "publish_to_queue", mocks["publish"])
    return mocks


def body(message):
    return json.dumps(message).encode("utf-8")


def channel():
    ch = Mock()
    ch.is_open = True
    return ch


def method(tag, redelivered=False):
    m = Mock()
    m.delivery_tag = tag
    m.redelivered = redelivered
    return m


def published_events(io_mocks):
    return [c.args[0] for c in io_mocks["publish"].call_args_list]


# Happy path --------------------------------------------------------------------

def test_successful_export_uploads_both_and_publishes_completed(io_mocks):
    ch = channel()
    export_consumer.callback(ch, method(1), Mock(), body(make_export_request()))

    assert [u["key"] for u in io_mocks["uploads"]] == [
        "exports/org-test/ws-test/tender-test/exp-test-1/tender-export-exp-test-1.xlsx",
        "exports/org-test/ws-test/tender-test/exp-test-1/tender-export-exp-test-1.docx",
    ]
    assert {u["bucket"] for u in io_mocks["uploads"]} == {"test-bucket-not-real"}
    assert io_mocks["uploads"][0]["contentType"] == export_consumer.XLSX_CONTENT_TYPE
    assert io_mocks["uploads"][1]["contentType"] == export_consumer.DOCX_CONTENT_TYPE
    workbook = load_workbook(BytesIO(io_mocks["uploads"][0]["data"]))
    assert workbook.sheetnames == ["Requirements", "Compliance Matrix", "Actions"]

    io_mocks["publish"].assert_called_once()
    event, queue = io_mocks["publish"].call_args.args
    assert queue == export_consumer.RESPONSE_QUEUE == "rfp-export-responses"
    assert event["eventType"] == "RFP_EXPORT_COMPLETED"
    assert event["status"] == "COMPLETED"
    for field, value in (("exportId", "exp-test-1"), ("organizationId", "org-test"),
                         ("workspaceId", "ws-test"), ("tenderId", "tender-test"),
                         ("correlationId", "corr-test"), ("requestEventId", "evt-test-1")):
        assert event[field] == value
    assert [a["type"] for a in event["artifacts"]] == ["EXCEL", "WORD"]
    assert event["warnings"] == {"mandatoryWithoutDecision": 1,
                                 "approvedSectionCount": 3, "excludedSectionCount": 6}
    assert event["counts"] == {"requirements": 3, "actions": 1}
    assert "url" not in json.dumps(event["artifacts"]).lower()
    ch.basic_ack.assert_called_once_with(delivery_tag=1)


def test_key_prefix_from_request_is_sanitised(io_mocks):
    request = make_export_request(output={"bucket": "b", "keyPrefix": "/tenant a/ex*ports/"})
    export_consumer.callback(channel(), method(2), Mock(), body(request))
    assert io_mocks["uploads"][0]["key"] == "tenant_a/ex_ports/tender-export-exp-test-1.xlsx"


def test_bucket_falls_back_to_env(io_mocks, monkeypatch):
    monkeypatch.setenv("S3_EXPORT_BUCKET", "env-test-bucket")
    request = make_export_request(output=None)
    export_consumer.callback(channel(), method(3), Mock(), body(request))
    assert {u["bucket"] for u in io_mocks["uploads"]} == {"env-test-bucket"}


def test_missing_bucket_publishes_failed_and_rejects(io_mocks):
    ch = channel()
    export_consumer.callback(ch, method(4), Mock(), body(make_export_request(output={})))
    io_mocks["upload"].assert_not_called()
    (event,) = published_events(io_mocks)
    assert event["eventType"] == "RFP_EXPORT_FAILED"
    assert event["error"]["code"] == "CONFIGURATION_ERROR"
    ch.basic_nack.assert_called_once_with(delivery_tag=4, requeue=False)


@pytest.mark.parametrize("prefix", ["../escape", "a/./b", "   "])
def test_unsafe_key_prefix_is_rejected(io_mocks, prefix):
    request = make_export_request(output={"bucket": "b", "keyPrefix": prefix})
    assert export_consumer.process_export_delivery(body(request), False) == export_consumer.REJECT
    io_mocks["upload"].assert_not_called()
    assert published_events(io_mocks)[0]["error"]["code"] == "INVALID_OUTPUT"


# Validation --------------------------------------------------------------------

def test_malformed_json_is_rejected_without_publish(io_mocks):
    ch = channel()
    export_consumer.callback(ch, method(5), Mock(), b"{not json")
    io_mocks["publish"].assert_not_called()
    io_mocks["upload"].assert_not_called()
    ch.basic_nack.assert_called_once_with(delivery_tag=5, requeue=False)


def test_wrong_event_type_publishes_failed_and_rejects(io_mocks):
    ch = channel()
    request = make_export_request(eventType="RFP_ANALYSIS_REQUESTED")
    export_consumer.callback(ch, method(6), Mock(), body(request))
    io_mocks["upload"].assert_not_called()
    (event,) = published_events(io_mocks)
    assert event["eventType"] == "RFP_EXPORT_FAILED"
    assert event["error"] == {"code": "INVALID_EVENT_TYPE",
                              "message": "eventType must be RFP_EXPORT_REQUESTED",
                              "retryable": False}
    ch.basic_nack.assert_called_once_with(delivery_tag=6, requeue=False)


@pytest.mark.parametrize("field", ["organizationId", "workspaceId", "tenderId"])
def test_missing_ids_publish_failed_when_export_id_present(io_mocks, field):
    request = make_export_request()
    del request[field]
    assert export_consumer.process_export_delivery(body(request), False) == export_consumer.REJECT
    (event,) = published_events(io_mocks)
    assert event["error"]["code"] == "MISSING_REQUIRED_FIELDS"
    assert field in event["error"]["message"]
    assert event[field] is None


def test_missing_export_id_rejects_without_publish(io_mocks):
    request = make_export_request()
    del request["exportId"]
    assert export_consumer.process_export_delivery(body(request), False) == export_consumer.REJECT
    io_mocks["publish"].assert_not_called()


def test_invalid_snapshot_publishes_failed(io_mocks):
    request = make_export_request(snapshot={"requirements": [{"content": 5}]})
    assert export_consumer.process_export_delivery(body(request), False) == export_consumer.REJECT
    assert published_events(io_mocks)[0]["error"]["code"] == "INVALID_SNAPSHOT"
    io_mocks["upload"].assert_not_called()


def test_failure_event_publish_error_requeues_once_then_rejects(io_mocks):
    io_mocks["publish"].side_effect = RuntimeError("broker down")
    request = make_export_request(eventType="WRONG")
    assert export_consumer.process_export_delivery(body(request), False) == export_consumer.REQUEUE
    assert export_consumer.process_export_delivery(body(request), True) == export_consumer.REJECT


# S3 / publish failures -------------------------------------------------------------

def test_upload_failure_requeues_first_and_never_publishes_completed(io_mocks):
    io_mocks["upload"].side_effect = RuntimeError("S3 down")
    ch = channel()
    export_consumer.callback(ch, method(7), Mock(), body(make_export_request()))
    io_mocks["publish"].assert_not_called()
    ch.basic_nack.assert_called_once_with(delivery_tag=7, requeue=True)


def test_redelivered_upload_failure_publishes_failed_and_rejects(io_mocks):
    io_mocks["upload"].side_effect = RuntimeError("S3 down secret-detail")
    ch = channel()
    export_consumer.callback(ch, method(8, redelivered=True), Mock(), body(make_export_request()))
    events = published_events(io_mocks)
    assert [e["eventType"] for e in events] == ["RFP_EXPORT_FAILED"]
    assert events[0]["error"]["code"] == "UPLOAD_FAILED"
    assert events[0]["error"]["retryable"] is True
    assert "secret-detail" not in json.dumps(events[0])
    ch.basic_nack.assert_called_once_with(delivery_tag=8, requeue=False)


def test_second_upload_failure_does_not_complete(io_mocks):
    calls = {"n": 0}
    original = io_mocks["upload"].side_effect

    def fail_second(*args):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("docx upload failed")
        return original(*args)

    io_mocks["upload"].side_effect = fail_second
    export_consumer.callback(channel(), method(9, redelivered=True), Mock(), body(make_export_request()))
    assert all(e["eventType"] != "RFP_EXPORT_COMPLETED" for e in published_events(io_mocks))


def test_completion_publish_failure_requeues_and_retry_reuses_uploads(io_mocks):
    io_mocks["publish"].side_effect = [RuntimeError("broker down"), None]
    first = channel()
    export_consumer.callback(first, method(10), Mock(), body(make_export_request()))
    first.basic_nack.assert_called_once_with(delivery_tag=10, requeue=True)
    assert io_mocks["upload"].call_count == 2

    second = channel()
    export_consumer.callback(second, method(11, redelivered=True), Mock(), body(make_export_request()))
    assert io_mocks["upload"].call_count == 2
    assert published_events(io_mocks)[-1]["eventType"] == "RFP_EXPORT_COMPLETED"
    second.basic_ack.assert_called_once_with(delivery_tag=11)


def test_changed_snapshot_before_completion_is_reuploaded(io_mocks):
    io_mocks["publish"].side_effect = [RuntimeError("broker down"), None]
    export_consumer.process_export_delivery(body(make_export_request()), False)
    changed = make_export_request()
    changed["snapshot"]["actions"] = []
    export_consumer.process_export_delivery(body(changed), True)
    assert io_mocks["upload"].call_count == 4


# Idempotency -------------------------------------------------------------------

def test_redelivery_of_completed_export_is_acked_without_reupload_or_republish(io_mocks):
    first = channel()
    first.basic_ack.side_effect = export_consumer.AMQPError("lost")
    export_consumer.callback(first, method(12), Mock(), body(make_export_request()))
    redelivery = channel()
    export_consumer.callback(redelivery, method(13, redelivered=True), Mock(), body(make_export_request()))

    assert io_mocks["upload"].call_count == 2
    assert io_mocks["publish"].call_count == 1
    redelivery.basic_ack.assert_called_once_with(delivery_tag=13)
    ledger = json.loads(export_consumer.EXPORT_LEDGER_PATH.read_text())
    assert ledger["exp-test-1"]["status"] == "COMPLETED"
    assert "Synthetic requirement" not in json.dumps(ledger)


def test_new_export_id_is_processed(io_mocks):
    export_consumer.process_export_delivery(body(make_export_request()), False)
    export_consumer.process_export_delivery(body(make_export_request(exportId="exp-test-2")), False)
    assert io_mocks["publish"].call_count == 2


# No analysis / LLM / RAG calls -------------------------------------------------

def test_export_never_calls_ollama_rag_or_http(io_mocks, monkeypatch):
    import requests
    import app.llm.ollama_client as ollama_client
    import app.rag.matcher as matcher
    import app.rag.retriever as retriever

    def forbidden(*args, **kwargs):
        raise AssertionError("forbidden call during export")

    monkeypatch.setattr(requests, "post", forbidden)
    monkeypatch.setattr(requests, "get", forbidden)
    monkeypatch.setattr(ollama_client, "generate_response", forbidden)
    monkeypatch.setattr(retriever, "TenderRetriever", forbidden)
    monkeypatch.setattr(matcher, "RequirementEvidenceMatcher", forbidden)
    monkeypatch.setattr(analysis_consumer, "process_tender_document", forbidden)

    assert export_consumer.process_export_delivery(
        body(make_export_request()), False
    ) == export_consumer.ACK
    assert published_events(io_mocks)[0]["eventType"] == "RFP_EXPORT_COMPLETED"


# Routing -----------------------------------------------------------------------

def test_export_consumer_uses_export_queues_prefetch_one_manual_ack(monkeypatch):
    connection = MagicMock()
    connection.is_open = True
    ch = connection.channel.return_value
    monkeypatch.setattr(export_consumer, "create_connection", lambda: connection)

    export_consumer._consume({"worker": None})

    ch.queue_declare.assert_called_once_with(queue="rfp-export-requests", durable=True)
    ch.basic_qos.assert_called_once_with(prefetch_count=1)
    kwargs = ch.basic_consume.call_args.kwargs
    assert kwargs["queue"] == "rfp-export-requests"
    assert kwargs["auto_ack"] is False
    assert export_consumer.REQUEST_QUEUE != analysis_consumer.QUEUE
    assert export_consumer.RESPONSE_QUEUE != publisher.RESPONSE_QUEUE


def test_publish_to_queue_confirms_and_uses_given_queue(monkeypatch):
    connection = MagicMock()
    connection.is_open = True
    ch = connection.channel.return_value
    monkeypatch.setattr(publisher, "create_connection", lambda: connection)
    order = []
    ch.confirm_delivery.side_effect = lambda: order.append("confirm")
    ch.basic_publish.side_effect = lambda **kw: order.append("publish")

    publisher.publish_to_queue({"eventType": "RFP_EXPORT_COMPLETED"}, "rfp-export-responses")

    ch.queue_declare.assert_called_once_with(queue="rfp-export-responses", durable=True)
    kwargs = ch.basic_publish.call_args.kwargs
    assert kwargs["routing_key"] == "rfp-export-responses"
    assert kwargs["mandatory"] is True
    assert kwargs["properties"].delivery_mode == 2
    assert order == ["confirm", "publish"]
    with pytest.raises(ValueError):
        publisher.publish_to_queue({}, "")


def test_analysis_publish_message_still_targets_analysis_queue(monkeypatch):
    connection = MagicMock()
    connection.is_open = True
    ch = connection.channel.return_value
    monkeypatch.setattr(publisher, "create_connection", lambda: connection)
    publisher.publish_message({"eventType": "RFP_ANALYSIS_COMPLETED"})
    assert ch.basic_publish.call_args.kwargs["routing_key"] == "rfp_docs_response1"


@pytest.mark.parametrize("event_type", [
    "RFP_EXPORT_REQUESTED", "rfp_export_requested", "RFP_EXPORT_COMPLETED",
])
def test_analysis_consumer_rejects_export_events_without_analysis(monkeypatch, tmp_path, event_type):
    forbidden = Mock(side_effect=AssertionError("must not run"))
    monkeypatch.setattr(analysis_consumer, "COMPLETION_LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(analysis_consumer, "download_file", forbidden)
    monkeypatch.setattr(analysis_consumer, "process_tender_document", forbidden)
    monkeypatch.setattr(analysis_consumer, "publish_message", forbidden)
    ch = channel()
    request = make_export_request(eventType=event_type)
    request["documents"] = [{"documentId": "d", "filename": "f.docx",
                             "bucket": "b", "key": "k"}]

    analysis_consumer.callback(ch, method(20), Mock(), body(request))

    forbidden.assert_not_called()
    ch.basic_ack.assert_not_called()
    ch.basic_nack.assert_called_once_with(delivery_tag=20, requeue=False)
    assert not (tmp_path / "l.json").exists()


# S3 helper ---------------------------------------------------------------------

def test_upload_bytes_puts_object_with_content_type(monkeypatch):
    fake = Mock()
    monkeypatch.setattr(s3_client, "create_s3_client", lambda: fake)
    result = s3_client.upload_bytes(b"abc", "test-bucket", "a/b.xlsx", "ct/x")
    fake.put_object.assert_called_once_with(
        Bucket="test-bucket", Key="a/b.xlsx", Body=b"abc", ContentType="ct/x"
    )
    assert result == {
        "bucket": "test-bucket", "key": "a/b.xlsx", "contentType": "ct/x",
        "sizeBytes": 3,
        "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    }


def test_upload_bytes_propagates_failure(monkeypatch):
    fake = Mock()
    fake.put_object.side_effect = RuntimeError("denied")
    monkeypatch.setattr(s3_client, "create_s3_client", lambda: fake)
    with pytest.raises(RuntimeError):
        s3_client.upload_bytes(b"abc", "test-bucket", "k", "ct")
    with pytest.raises(ValueError):
        s3_client.upload_bytes(b"abc", "", "k", "ct")


# Worker thread -----------------------------------------------------------------

def test_worker_runs_off_io_thread_and_settles_threadsafe(io_mocks):
    seen = []
    io_mocks["publish"].side_effect = lambda *a: seen.append(threading.current_thread())
    scheduled = []
    connection = Mock()
    connection.add_callback_threadsafe.side_effect = scheduled.append
    ch = channel()
    state = {"worker": None}

    export_consumer._start_delivery_worker(connection, ch, method(30), body(make_export_request()), state)
    state["worker"].join(timeout=10)

    assert seen and seen[0] is not threading.main_thread()
    ch.basic_ack.assert_not_called()
    scheduled[0]()
    ch.basic_ack.assert_called_once_with(delivery_tag=30)


def test_worker_waits_configured_delay_before_requeue(io_mocks, monkeypatch):
    monkeypatch.setenv("EXPORT_REQUEUE_DELAY_SECONDS", "1.5")
    io_mocks["upload"].side_effect = RuntimeError("S3 down")
    order = []
    monkeypatch.setattr(export_consumer.time, "sleep", lambda s: order.append(("sleep", s)))
    connection = Mock()
    connection.add_callback_threadsafe.side_effect = lambda cb: order.append(("settle", cb))
    ch = channel()
    state = {"worker": None}

    export_consumer._start_delivery_worker(connection, ch, method(31), body(make_export_request()), state)
    state["worker"].join(timeout=10)

    assert [kind for kind, _ in order] == ["sleep", "settle"]
    assert order[0][1] == 1.5
    order[1][1]()
    ch.basic_nack.assert_called_once_with(delivery_tag=31, requeue=True)


@pytest.mark.parametrize("raw, expected", [
    (None, 5.0), ("", 5.0), ("2.5", 2.5), ("0", 0.0), ("abc", 5.0),
    ("-1", 5.0), ("nan", 5.0), ("9999", 300.0),
])
def test_requeue_delay_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("EXPORT_REQUEUE_DELAY_SECONDS", raising=False)
    else:
        monkeypatch.setenv("EXPORT_REQUEUE_DELAY_SECONDS", raw)
    assert export_consumer.requeue_delay_seconds() == expected


# Queue configuration safety -------------------------------------------------------

def test_default_queue_configuration_is_valid():
    export_consumer.validate_queue_configuration(
        "rfp-export-requests", "rfp-export-responses", "rfp-python-test"
    )


@pytest.mark.parametrize("request_queue, response_queue, analysis_queue, needle", [
    ("rfp-python-test", "rfp-export-responses", "rfp-python-test", "RABBITMQ_QUEUE"),
    ("same", "same", "rfp-python-test", "RABBITMQ_EXPORT_RESPONSE_QUEUE"),
    ("rfp-export-requests", "rfp_docs_response1", "rfp-python-test", "analysis response"),
    ("rfp-export-requests", "copilot-analysis", "copilot-analysis", "RABBITMQ_QUEUE"),
    ("rfp_docs_response1", "rfp-export-responses", "rfp-python-test", "analysis response"),
    (" rfp-python-test ", "rfp-export-responses", "rfp-python-test", "RABBITMQ_QUEUE"),
    ("", "rfp-export-responses", "rfp-python-test", "blank"),
    ("rfp-export-requests", "  ", "rfp-python-test", "blank"),
])
def test_conflicting_queue_configuration_is_refused(
    request_queue, response_queue, analysis_queue, needle
):
    with pytest.raises(export_consumer.ExportConfigurationError) as excinfo:
        export_consumer.validate_queue_configuration(
            request_queue, response_queue, analysis_queue
        )
    assert needle in str(excinfo.value)


def test_startup_refuses_export_queue_equal_to_analysis_queue_before_connecting(monkeypatch):
    monkeypatch.setenv("RABBITMQ_QUEUE", "copilot-analysis")
    monkeypatch.setattr(export_consumer, "REQUEST_QUEUE", "copilot-analysis")
    # create_connection raises AssertionError (fixture) if it is reached.
    assert export_consumer.start_export_consumer() == export_consumer.EXIT_CONFIGURATION_ERROR


def test_startup_uses_analysis_default_queue_when_env_unset(monkeypatch):
    monkeypatch.delenv("RABBITMQ_QUEUE", raising=False)
    monkeypatch.setattr(export_consumer, "REQUEST_QUEUE", "rfp-python-test")
    assert export_consumer.start_export_consumer() == export_consumer.EXIT_CONFIGURATION_ERROR


def test_startup_refuses_export_response_equal_to_analysis_response(monkeypatch):
    monkeypatch.setenv("RABBITMQ_QUEUE", "analysis-test-queue")
    monkeypatch.setattr(export_consumer, "RESPONSE_QUEUE", publisher.RESPONSE_QUEUE)
    assert export_consumer.start_export_consumer() == export_consumer.EXIT_CONFIGURATION_ERROR


# Bucket resolution ----------------------------------------------------------------

def test_bucket_falls_back_to_s3_bucket_and_logs_only_the_source(io_mocks, monkeypatch, capsys):
    monkeypatch.setenv("S3_BUCKET", "fallback-test-bucket")
    export_consumer.callback(channel(), method(40), Mock(), body(make_export_request(output=None)))
    assert {u["bucket"] for u in io_mocks["uploads"]} == {"fallback-test-bucket"}
    out = capsys.readouterr().out
    assert "Export bucket source: S3_BUCKET" in out
    assert "fallback-test-bucket" not in out


def test_s3_export_bucket_wins_over_s3_bucket(io_mocks, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "fallback-test-bucket")
    monkeypatch.setenv("S3_EXPORT_BUCKET", "export-test-bucket")
    export_consumer.callback(channel(), method(41), Mock(), body(make_export_request(output={})))
    assert {u["bucket"] for u in io_mocks["uploads"]} == {"export-test-bucket"}


def test_request_bucket_wins_over_env(io_mocks, monkeypatch, capsys):
    monkeypatch.setenv("S3_BUCKET", "fallback-test-bucket")
    monkeypatch.setenv("S3_EXPORT_BUCKET", "export-test-bucket")
    export_consumer.callback(channel(), method(42), Mock(), body(make_export_request()))
    assert {u["bucket"] for u in io_mocks["uploads"]} == {"test-bucket-not-real"}
    assert "Export bucket source: output.bucket" in capsys.readouterr().out


def test_blank_request_bucket_is_rejected_even_with_env(io_mocks, monkeypatch):
    monkeypatch.setenv("S3_BUCKET", "fallback-test-bucket")
    request = make_export_request(output={"bucket": "  "})
    assert export_consumer.process_export_delivery(body(request), False) == export_consumer.REJECT
    io_mocks["upload"].assert_not_called()
    assert published_events(io_mocks)[0]["error"]["code"] == "INVALID_OUTPUT"


def test_blank_env_buckets_are_ignored(io_mocks, monkeypatch):
    monkeypatch.setenv("S3_EXPORT_BUCKET", " ")
    monkeypatch.setenv("S3_BUCKET", "")
    request = make_export_request(output=None)
    assert export_consumer.process_export_delivery(body(request), False) == export_consumer.REJECT
    assert published_events(io_mocks)[0]["error"]["code"] == "CONFIGURATION_ERROR"


# Ledger reliability ---------------------------------------------------------------

UNREADABLE_LEDGERS = [
    pytest.param(b"{not json", id="invalid-json"),
    pytest.param(b"[]", id="list"),
    pytest.param(b'"text"', id="string"),
    pytest.param(b'{"exp-other": "not-an-object"}', id="bad-entry"),
    pytest.param(b"\xff\xfe\x00garbage", id="invalid-utf8"),
]


def _corrupt_copies():
    path = export_consumer.EXPORT_LEDGER_PATH
    return sorted(path.parent.glob(path.name + ".corrupt-*"))


@pytest.mark.parametrize("raw", UNREADABLE_LEDGERS)
def test_unreadable_ledger_is_preserved_and_nothing_is_processed(io_mocks, raw):
    path = export_consumer.EXPORT_LEDGER_PATH
    path.write_bytes(raw)

    outcome = export_consumer.process_export_delivery(body(make_export_request()), False)

    assert outcome == export_consumer.REQUEUE_AND_STOP
    assert path.read_bytes() == raw
    io_mocks["upload"].assert_not_called()
    io_mocks["publish"].assert_not_called()
    (copy,) = _corrupt_copies()
    assert copy.read_bytes() == raw


def test_unreadable_ledger_path_is_left_untouched(io_mocks):
    path = export_consumer.EXPORT_LEDGER_PATH
    path.mkdir()
    (path / "marker").write_text("keep")

    outcome = export_consumer.process_export_delivery(body(make_export_request()), True)

    assert outcome == export_consumer.REQUEUE_AND_STOP
    assert (path / "marker").read_text() == "keep"
    io_mocks["upload"].assert_not_called()
    io_mocks["publish"].assert_not_called()


def test_unreadable_ledger_requeues_and_stops_consuming(io_mocks):
    export_consumer.EXPORT_LEDGER_PATH.write_bytes(b"{broken")
    ch = channel()
    export_consumer.callback(ch, method(50), Mock(), body(make_export_request()))
    ch.basic_ack.assert_not_called()
    ch.basic_nack.assert_called_once_with(delivery_tag=50, requeue=True)
    ch.stop_consuming.assert_called_once()


def test_worker_thread_marks_state_fatal_on_unreadable_ledger(io_mocks):
    export_consumer.EXPORT_LEDGER_PATH.write_bytes(b"{broken")
    scheduled = []
    connection = Mock()
    connection.add_callback_threadsafe.side_effect = scheduled.append
    ch = channel()
    state = {"worker": None}

    export_consumer._start_delivery_worker(connection, ch, method(51), body(make_export_request()), state)
    state["worker"].join(timeout=10)

    assert state["fatal"] is True
    scheduled[0]()
    ch.basic_nack.assert_called_once_with(delivery_tag=51, requeue=True)
    ch.stop_consuming.assert_called_once()


def test_consumer_exits_with_ledger_code_after_fatal_stop(monkeypatch):
    monkeypatch.setenv("RABBITMQ_QUEUE", "analysis-test-queue")
    monkeypatch.setattr(export_consumer, "_consume", lambda state: state.update(fatal=True))
    assert export_consumer.start_export_consumer() == export_consumer.EXIT_LEDGER_UNAVAILABLE


def test_startup_refuses_unreadable_ledger_before_connecting(monkeypatch):
    monkeypatch.setenv("RABBITMQ_QUEUE", "analysis-test-queue")
    path = export_consumer.EXPORT_LEDGER_PATH
    path.write_bytes(b"[1, 2]")
    assert export_consumer.start_export_consumer() == export_consumer.EXIT_LEDGER_UNAVAILABLE
    assert path.read_bytes() == b"[1, 2]"


def test_startup_with_missing_ledger_proceeds_to_connect(monkeypatch):
    monkeypatch.setenv("RABBITMQ_QUEUE", "analysis-test-queue")
    connected = []
    monkeypatch.setattr(export_consumer, "_consume", lambda state: connected.append(True))
    assert export_consumer.start_export_consumer() is None
    assert connected == [True]
    assert not export_consumer.EXPORT_LEDGER_PATH.exists()


def test_missing_ledger_is_an_empty_ledger(io_mocks):
    assert not export_consumer.EXPORT_LEDGER_PATH.exists()
    assert export_consumer.process_export_delivery(body(make_export_request()), False) == export_consumer.ACK
    ledger = json.loads(export_consumer.EXPORT_LEDGER_PATH.read_text())
    assert ledger["exp-test-1"]["status"] == "COMPLETED"
    assert _corrupt_copies() == []


def test_ledger_corrupted_after_completion_publish_is_not_overwritten(io_mocks):
    path = export_consumer.EXPORT_LEDGER_PATH

    def corrupt_ledger(*args):
        path.write_bytes(b"{corrupted mid-flight")

    io_mocks["publish"].side_effect = corrupt_ledger
    outcome = export_consumer.process_export_delivery(body(make_export_request()), False)

    assert outcome == export_consumer.ACK_AND_STOP
    assert path.read_bytes() == b"{corrupted mid-flight"
    ch = channel()
    export_consumer.settle_delivery(ch, 52, outcome)
    ch.basic_ack.assert_called_once_with(delivery_tag=52)
    ch.stop_consuming.assert_called_once()


def test_ledger_lock_file_is_next_to_ledger(io_mocks):
    export_consumer.process_export_delivery(body(make_export_request()), False)
    path = export_consumer.EXPORT_LEDGER_PATH
    if export_consumer.fcntl is not None:
        assert (path.parent / (path.name + ".lock")).exists()


# Export ID conflicts ----------------------------------------------------------------

def test_completed_export_id_with_changed_snapshot_publishes_conflict(io_mocks):
    export_consumer.process_export_delivery(body(make_export_request()), False)
    ledger_before = export_consumer.EXPORT_LEDGER_PATH.read_bytes()
    changed = make_export_request()
    changed["snapshot"]["actions"] = []
    ch = channel()

    export_consumer.callback(ch, method(60), Mock(), body(changed))

    assert io_mocks["upload"].call_count == 2
    events = published_events(io_mocks)
    assert [e["eventType"] for e in events] == ["RFP_EXPORT_COMPLETED", "RFP_EXPORT_FAILED"]
    assert events[1]["error"]["code"] == "EXPORT_ID_CONFLICT"
    assert events[1]["error"]["retryable"] is False
    assert events[1]["exportId"] == "exp-test-1"
    assert events[1]["artifacts"] == []
    ch.basic_nack.assert_called_once_with(delivery_tag=60, requeue=False)
    assert export_consumer.EXPORT_LEDGER_PATH.read_bytes() == ledger_before


def test_conflict_event_publish_failure_requeues_once(io_mocks):
    export_consumer.process_export_delivery(body(make_export_request()), False)
    io_mocks["publish"].side_effect = RuntimeError("broker down")
    changed = make_export_request()
    changed["snapshot"]["actions"] = []
    assert export_consumer.process_export_delivery(body(changed), False) == export_consumer.REQUEUE
    assert export_consumer.process_export_delivery(body(changed), True) == export_consumer.REJECT
    assert io_mocks["upload"].call_count == 2


def test_same_export_id_same_snapshot_is_still_a_silent_duplicate(io_mocks):
    export_consumer.process_export_delivery(body(make_export_request()), False)
    resent = make_export_request(eventId="evt-test-resent", correlationId="corr-other")
    assert export_consumer.process_export_delivery(body(resent), True) == export_consumer.ACK
    assert io_mocks["upload"].call_count == 2
    assert io_mocks["publish"].call_count == 1


# Event contract ---------------------------------------------------------------------

TIMESTAMP_RE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"


def test_timestamps_use_seconds_precision_utc(io_mocks):
    import re
    export_consumer.process_export_delivery(body(make_export_request()), False)
    export_consumer.process_export_delivery(body(make_export_request(exportId="x", eventType="BAD")), False)
    completed, failed = published_events(io_mocks)
    assert re.match(TIMESTAMP_RE, completed["completedAt"])
    assert re.match(TIMESTAMP_RE, failed["failedAt"])
    ledger = json.loads(export_consumer.EXPORT_LEDGER_PATH.read_text())
    assert re.match(TIMESTAMP_RE, ledger["exp-test-1"]["updatedAt"])


@pytest.mark.parametrize("attempt, expected", [
    ("absent", None), (None, None), (3, 3), ("2", None), (True, None), (1.0, None),
])
def test_attempt_is_echoed_only_when_integer(io_mocks, attempt, expected):
    request = make_export_request()
    if attempt == "absent":
        del request["attempt"]
    else:
        request["attempt"] = attempt
    export_consumer.process_export_delivery(body(request), False)
    assert published_events(io_mocks)[0]["attempt"] == expected


# Error classification and privacy -------------------------------------------------

def test_invalid_snapshot_error_names_paths_but_not_content(io_mocks):
    request = make_export_request(snapshot={"requirements": [
        {"requirementId": "SECRET-REQ-ID", "content": "SECRET TENDER TEXT", "mandatory": "SECRET-FLAG"},
    ]})
    export_consumer.process_export_delivery(body(request), False)
    (event,) = published_events(io_mocks)
    assert event["error"] == {
        "code": "INVALID_SNAPSHOT",
        "message": "snapshot.requirements[0].mandatory must be a boolean or null",
        "retryable": False,
    }
    assert "SECRET" not in json.dumps(event)


def test_render_failure_is_not_retryable_and_hides_exception_text(io_mocks, monkeypatch, capsys):
    def explode(snapshot):
        raise ValueError("SECRET TENDER TEXT in exception")

    monkeypatch.setattr(export_consumer, "render_excel", explode)
    outcome = export_consumer.process_export_delivery(body(make_export_request()), False)
    assert outcome == export_consumer.REJECT
    (event,) = published_events(io_mocks)
    assert event["error"] == {"code": "RENDER_FAILED",
                              "message": "Export rendering failed (ValueError)",
                              "retryable": False}
    io_mocks["upload"].assert_not_called()
    assert "SECRET" not in capsys.readouterr().out


def test_invalid_characters_in_snapshot_still_export(io_mocks):
    request = make_export_request()
    request["snapshot"]["requirements"][0]["content"] = "a\ud800b\ufffec\uffffd\x0be"
    request["snapshot"]["draftSections"][0]["content"] = "x\udfffy\x0cz"
    assert export_consumer.process_export_delivery(body(request), False) == export_consumer.ACK
    assert published_events(io_mocks)[0]["eventType"] == "RFP_EXPORT_COMPLETED"


def test_invalid_characters_in_correlation_field_are_not_retryable(io_mocks):
    request = make_export_request(correlationId="corr-\ud800")
    outcome = export_consumer.process_export_delivery(body(request), False)
    assert outcome == export_consumer.REJECT
    (event,) = published_events(io_mocks)
    assert event["error"]["code"] == "INVALID_FIELD_VALUE"
    assert event["error"]["retryable"] is False
    assert event["correlationId"] is None
    json.dumps(event, ensure_ascii=False).encode("utf-8")
    io_mocks["upload"].assert_not_called()


def test_upload_and_publish_failures_remain_retryable(io_mocks):
    io_mocks["publish"].side_effect = [RuntimeError("down"), None]
    export_consumer.process_export_delivery(body(make_export_request()), True)
    attempted, event = published_events(io_mocks)
    assert attempted["eventType"] == "RFP_EXPORT_COMPLETED"
    assert event["error"]["code"] == "PUBLISH_FAILED"
    assert event["error"]["retryable"] is True
