import json
import threading
import time

import pytest

import app.llm.ollama_client as ollama_client
import app.orchestration.service as service
from app.orchestration.service import process_tender_document


CHUNK_COUNT = 74


def _chunks(count=CHUNK_COUNT):
    return [
        {
            "page": index,
            "text": f"Chunk {index} text: the vendor shall do task {index}.",
            "source_type": "table" if index % 2 else "text",
            "table_index": index % 3,
            "page_reference_type": "physical",
        }
        for index in range(1, count + 1)
    ]


def _requirement_payload(chunk):
    return {
        "requirements": [
            {
                "text": f"Requirement A from {chunk['text']}",
                "category": "Functional",
                "mandatory": True,
                "confidence": "High",
            },
            {
                "text": f"Requirement B from {chunk['text']}",
                "category": "Technical",
                "mandatory": False,
                "confidence": "Medium",
            },
        ]
    }


class RecordingMatcher:
    def __init__(self):
        self.calls = []

    def match_requirements(self, requirements, top_k=3):
        self.calls.append((
            threading.current_thread() is threading.main_thread(),
            requirements[0]["source"]["chunk"],
        ))
        return [
            {"status": "NOT_FOUND", "evidence": [], "note": "Matcher test"}
            for _ in requirements
        ]


@pytest.fixture
def setup_document(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "PROGRESS_DIR", str(tmp_path / "progress"))
    monkeypatch.setattr(service, "OUTPUT_DIR", str(tmp_path / "output"))

    def configure(chunks, extract, workers):
        monkeypatch.setenv(service.EXTRACT_MAX_WORKERS_ENV, str(workers))
        monkeypatch.setattr(
            service, "extract_pdf_text",
            lambda path: [{"page": 1, "text": "Tender text"}],
        )
        monkeypatch.setattr(service, "create_chunks", lambda pages: chunks)
        monkeypatch.setattr(service, "extract_requirements", extract)
        matcher = RecordingMatcher()
        monkeypatch.setattr(
            service, "RequirementEvidenceMatcher", lambda: matcher,
        )
        return matcher

    return configure


def _assert_ordered_and_traceable(result, chunks):
    assert len(result) == 2 * len(chunks)
    for position, chunk in enumerate(chunks):
        first, second = result[2 * position], result[2 * position + 1]
        assert first["text"] == f"Requirement A from {chunk['text']}"
        assert second["text"] == f"Requirement B from {chunk['text']}"
        for item in (first, second):
            assert item["source"] == {
                "page": chunk["page"],
                "chunk": position + 1,
                "source_type": chunk["source_type"],
                "table_index": chunk["table_index"],
                "excerpt": chunk["text"],
                "confidence": item["confidence"],
                "page_reference_type": chunk["page_reference_type"],
            }
            assert set(item["processing_metrics"]) == {
                "requirement_extraction_seconds"
            }
            assert item["evidence_match"]["status"] == "NOT_FOUND"


@pytest.mark.parametrize("workers", [2, 4])
def test_parallel_preserves_all_chunks_in_original_order(
    setup_document, workers
):
    chunks = _chunks()

    def extract(chunk):
        # Later chunks finish first so completion order differs from index order.
        time.sleep(0.002 * ((CHUNK_COUNT - chunk["page"]) % 5))
        return _requirement_payload(chunk)

    matcher = setup_document(chunks, extract, workers)

    result = process_tender_document("tender.pdf")

    _assert_ordered_and_traceable(result, chunks)
    assert [chunk for _main, chunk in matcher.calls] == list(
        range(1, CHUNK_COUNT + 1)
    )
    assert all(on_main for on_main, _chunk in matcher.calls)


def test_parallel_output_matches_sequential_output(setup_document):
    chunks = _chunks(12)

    def extract(chunk):
        return _requirement_payload(chunk)

    def normalized(result):
        for item in result:
            item["processing_metrics"] = None
        return result

    setup_document(chunks, extract, 1)
    sequential = normalized(process_tender_document("sequential.pdf"))
    setup_document(chunks, extract, 2)
    parallel = normalized(process_tender_document("parallel.pdf"))

    assert parallel == sequential


def test_two_workers_actually_overlap_extraction(setup_document):
    chunks = _chunks(2)
    barrier = threading.Barrier(2, timeout=5)

    def extract(chunk):
        # Raises BrokenBarrierError unless both chunks are in flight together.
        barrier.wait()
        return _requirement_payload(chunk)

    setup_document(chunks, extract, 2)

    _assert_ordered_and_traceable(process_tender_document("tender.pdf"), chunks)


def test_sequential_mode_never_overlaps(setup_document):
    chunks = _chunks(6)
    lock = threading.Lock()
    state = {"active": 0, "peak": 0, "threads": set()}

    def extract(chunk):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["threads"].add(threading.current_thread().name)
        time.sleep(0.005)
        with lock:
            state["active"] -= 1
        return _requirement_payload(chunk)

    setup_document(chunks, extract, 1)

    _assert_ordered_and_traceable(process_tender_document("tender.pdf"), chunks)
    assert state["peak"] == 1
    assert state["threads"] == {threading.main_thread().name}


def test_default_workers_is_one(monkeypatch):
    monkeypatch.delenv(service.EXTRACT_MAX_WORKERS_ENV, raising=False)
    assert service.extract_max_workers() == 1


@pytest.mark.parametrize("raw", ["0", "-1", "two"])
def test_invalid_worker_setting_is_rejected(monkeypatch, raw):
    monkeypatch.setenv(service.EXTRACT_MAX_WORKERS_ENV, raw)
    with pytest.raises(ValueError, match=service.EXTRACT_MAX_WORKERS_ENV):
        service.extract_max_workers()


def _progress_chunks(tmp_path):
    (progress_file,) = (tmp_path / "progress").glob("*.json")
    return json.loads(progress_file.read_text(encoding="utf-8"))[
        "completed_chunks"
    ]


def test_failed_chunk_keeps_successful_results_and_resumes_in_order(
    setup_document, tmp_path
):
    chunks = _chunks(6)
    calls = []
    calls_lock = threading.Lock()
    chunk_three_done = threading.Event()
    fail_once = {"armed": True}

    def extract(chunk):
        page = chunk["page"]
        with calls_lock:
            calls.append(page)
        if page == 2 and fail_once["armed"]:
            fail_once["armed"] = False
            # Fail only after a later chunk has finished, so the checkpoint
            # holds an out-of-order chunk when resume runs.
            assert chunk_three_done.wait(timeout=5)
            raise RuntimeError("Simulated chunk 2 failure")
        payload = _requirement_payload(chunk)
        if page == 3:
            chunk_three_done.set()
        return payload

    setup_document(chunks, extract, 2)

    with pytest.raises(RuntimeError, match="Simulated chunk 2 failure"):
        process_tender_document("tender.pdf")

    saved = _progress_chunks(tmp_path)
    assert "1" in saved and "3" in saved
    assert "2" not in saved
    assert not list((tmp_path / "output").glob("*.json"))
    first_run_calls = list(calls)

    result = process_tender_document("tender.pdf")

    _assert_ordered_and_traceable(result, chunks)
    resumed_calls = calls[len(first_run_calls):]
    assert 2 in resumed_calls
    # Chunks checkpointed by the failed run are never extracted again.
    assert not set(saved) & {str(page) for page in resumed_calls}
    assert len(list((tmp_path / "output").glob("*.json"))) == 1


def test_validation_failure_in_parallel_mode_raises_original_error(
    setup_document, tmp_path
):
    chunks = _chunks(3)

    def extract(chunk):
        if chunk["page"] == 2:
            return "not valid JSON"
        return _requirement_payload(chunk)

    setup_document(chunks, extract, 2)

    with pytest.raises(ValueError, match="Invalid JSON returned for chunk 2"):
        process_tender_document("tender.pdf")

    assert "1" in _progress_chunks(tmp_path)
    assert "2" not in _progress_chunks(tmp_path)


def test_failure_cancels_queued_chunks(setup_document, tmp_path):
    chunks = _chunks(20)
    started = []
    started_lock = threading.Lock()

    def extract(chunk):
        with started_lock:
            started.append(chunk["page"])
        if chunk["page"] == 1:
            raise RuntimeError("Simulated chunk 1 failure")
        time.sleep(0.02)
        return _requirement_payload(chunk)

    setup_document(chunks, extract, 2)

    with pytest.raises(RuntimeError, match="Simulated chunk 1 failure"):
        process_tender_document("tender.pdf")

    # Queued work is cancelled instead of running all remaining chunks.
    assert len(started) < len(chunks)
    saved = _progress_chunks(tmp_path) if list(
        (tmp_path / "progress").glob("*.json")
    ) else {}
    assert "1" not in saved


def test_all_requirements_orders_by_numeric_chunk_index():
    progress = {
        "10": [{"text": "ten"}],
        "2": [{"text": "two"}],
        "1": [{"text": "one"}],
    }
    assert [item["text"] for item in service._all_requirements(progress)] == [
        "one", "two", "ten",
    ]


def test_extract_cache_is_thread_safe_and_returns_independent_copies(
    monkeypatch,
):
    ollama_client.clear_extract_requirements_cache()
    generate_calls = []
    calls_lock = threading.Lock()

    def fake_generate(prompt, **kwargs):
        with calls_lock:
            generate_calls.append(kwargs.get("label"))
        time.sleep(0.001)
        return json.dumps({
            "requirements": [{
                "text": "The vendor shall provide support.",
                "category": "Functional",
                "mandatory": True,
                "confidence": "High",
            }]
        })

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    distinct = 8
    results = [None] * (distinct * 6)
    errors = []

    def worker(slot):
        try:
            text = f"Section {slot % distinct}: the vendor shall provide support."
            results[slot] = ollama_client.extract_requirements({"text": text})
        except Exception as exc:  # pragma: no cover - surfaced by assert
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(slot,))
        for slot in range(len(results))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert all(len(result["requirements"]) == 1 for result in results)
    assert distinct <= len(generate_calls) <= len(results)

    results[0]["requirements"][0]["text"] = "mutated by caller"
    again = ollama_client.extract_requirements(
        {"text": "Section 0: the vendor shall provide support."}
    )
    assert again["requirements"][0]["text"] == (
        "The vendor shall provide support."
    )
    ollama_client.clear_extract_requirements_cache()


def test_generate_metrics_are_per_thread():
    barrier = threading.Barrier(2, timeout=5)
    seen = {}

    def worker(label, done_reason):
        ollama_client._log_generate_metrics(
            label, 0.01, {"response": "{}", "done_reason": done_reason},
            2, 16,
        )
        barrier.wait()
        seen[label] = ollama_client.get_last_generate_metrics()

    threads = [
        threading.Thread(target=worker, args=("extract_requirements", "stop")),
        threading.Thread(
            target=worker,
            args=("assess_evidence_relevance_batch", "length"),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert seen["extract_requirements"]["label"] == "extract_requirements"
    assert seen["extract_requirements"]["done_reason"] == "stop"
    assert seen["assess_evidence_relevance_batch"]["done_reason"] == "length"
