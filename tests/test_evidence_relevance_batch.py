"""Unit tests for batched evidence relevance parsing and fallback."""

import json

import pytest
import requests

from app.llm import ollama_client
from app.rag.matcher import RequirementEvidenceMatcher


def test_batch_valid_response_with_pair_index(monkeypatch):
    calls = []

    def fake_generate(prompt, **kwargs):
        calls.append(kwargs.get("label"))
        return json.dumps({
            "assessments": [
                {"pair_index": 0, "relevant": True, "reason": "Same capability"},
                {"pair_index": 1, "relevant": False, "reason": "Different topic"},
            ]
        })

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(
        ollama_client,
        "assess_evidence_relevance",
        lambda *_: (_ for _ in ()).throw(AssertionError("no sequential fallback")),
    )

    results = ollama_client.assess_evidence_relevance_batch([
        ("Provide fleet tracking.", "CBS delivered fleet vehicle tracking."),
        ("Provide SSO.", "CBS signed the attendance sheet."),
    ])

    assert calls == ["assess_evidence_relevance_batch"]
    assert results == [
        {"relevant": True, "reason": "Same capability"},
        {"relevant": False, "reason": "Different topic"},
    ]


def test_batch_prompt_requires_one_assessment_per_pair_with_two_object_example(
    monkeypatch,
):
    """Prompt-only guard: schema example must show two assessments, not one."""
    captured = {}

    def fake_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        return json.dumps({
            "assessments": [
                {"pair_index": 0, "relevant": True, "reason": "a"},
                {"pair_index": 1, "relevant": False, "reason": "b"},
            ]
        })

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(
        ollama_client,
        "assess_evidence_relevance",
        lambda *_: (_ for _ in ()).throw(AssertionError("no sequential")),
    )

    ollama_client.assess_evidence_relevance_batch([
        ("Req A", "Ev A"),
        ("Req B", "Ev B"),
    ])

    prompt = captured["prompt"]
    assert "exactly one assessment object for every numbered PAIR" in prompt
    assert "assessments length must equal the number of PAIR blocks" in prompt
    assert "relevant as a JSON boolean" in prompt
    assert '"pair_index": 0' in prompt
    assert '"pair_index": 1' in prompt
    assert '"relevant": true' in prompt
    assert '"relevant": false' in prompt
    # Example must illustrate two objects (not a single-item schema sample).
    example_start = prompt.index("Example shape for two pairs:")
    example_end = prompt.index("PAIRS:")
    example = prompt[example_start:example_end]
    assert example.count('"pair_index"') == 2


def test_batch_accepts_out_of_order_pair_indexes(monkeypatch):
    def fake_generate(prompt, **kwargs):
        return json.dumps({
            "assessments": [
                {"pair_index": 1, "relevant": False, "reason": "No"},
                {"pair_index": 0, "relevant": True, "reason": "Yes"},
            ]
        })

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    results = ollama_client.assess_evidence_relevance_batch([
        ("Req A", "Evidence A about Req A"),
        ("Req B", "Evidence B unrelated"),
    ])
    assert results[0]["relevant"] is True
    assert results[1]["relevant"] is False


def test_relevance_batch_env_helpers_read_at_call_time(monkeypatch):
    monkeypatch.delenv("OLLAMA_RELEVANCE_BATCH_TIMEOUT", raising=False)
    monkeypatch.delenv("OLLAMA_RELEVANCE_BATCH_MAX_PAIRS", raising=False)
    assert ollama_client.relevance_batch_timeout_seconds() == 120
    assert ollama_client.relevance_batch_max_pairs() == 0
    monkeypatch.setenv("OLLAMA_RELEVANCE_BATCH_TIMEOUT", "45")
    monkeypatch.setenv("OLLAMA_RELEVANCE_BATCH_MAX_PAIRS", "3")
    assert ollama_client.relevance_batch_timeout_seconds() == 45
    assert ollama_client.relevance_batch_max_pairs() == 3


def test_batch_timeout_leaves_pairs_unassessed_without_sequential(monkeypatch, capsys):
    sequential = []

    def fake_generate(prompt, **kwargs):
        raise requests.Timeout("read timed out")

    def fake_single(requirement, evidence_text):
        sequential.append((requirement, evidence_text))
        return {"relevant": True, "reason": "must not run"}

    ollama_client.clear_batch_diagnostic_events()
    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(ollama_client, "assess_evidence_relevance", fake_single)

    results = ollama_client.assess_evidence_relevance_batch([
        ("Provide fleet tracking.", "fleet evidence"),
        ("Provide SSO.", "sso evidence"),
        ("Provide GPS.", "gps evidence"),
    ])

    assert sequential == []
    assert len(results) == 3
    assert all(item["relevant"] is False for item in results)
    assert all(item.get("unassessed") is True for item in results)
    assert all(
        "timed out" in item["reason"].casefold() for item in results
    )
    captured = capsys.readouterr().out
    assert "OLLAMA_RELEVANCE_BATCH timeout" in captured
    assert "sequential_pairs=0" in captured
    assert "skipped_unassessed=3" in captured
    events = ollama_client.get_batch_diagnostic_events()
    assert len(events) == 1
    assert events[0]["map_status"] == "timeout"
    assert events[0]["timed_out"] is True
    assert events[0]["missing"] == 3
    assert events[0]["reused"] == 0


def test_batch_max_pairs_split_uses_smaller_batches(monkeypatch):
    batch_sizes = []

    def fake_generate(prompt, **kwargs):
        # Count numbered pair headers only (not the instruction text).
        size = sum(
            1 for line in prompt.splitlines() if line.startswith("PAIR ")
        )
        batch_sizes.append(size)
        assessments = [
            {"pair_index": i, "relevant": False, "reason": "split"}
            for i in range(size)
        ]
        return json.dumps({"assessments": assessments})

    monkeypatch.setenv("OLLAMA_RELEVANCE_BATCH_MAX_PAIRS", "2")
    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(
        ollama_client,
        "assess_evidence_relevance",
        lambda *_: (_ for _ in ()).throw(AssertionError("no sequential")),
    )

    results = ollama_client.assess_evidence_relevance_batch([
        ("R1", "E1"),
        ("R2", "E2"),
        ("R3", "E3"),
        ("R4", "E4"),
    ])

    assert batch_sizes == [2, 2]
    assert len(results) == 4
    assert all(item["relevant"] is False for item in results)


def test_matcher_does_not_cache_or_accept_unassessed_timeout(monkeypatch, tmp_path):
    def fake_batch(pairs):
        return [
            {
                "relevant": False,
                "reason": ollama_client.BATCH_TIMEOUT_UNASSESSED_REASON,
                "unassessed": True,
            }
            for _ in pairs
        ]

    monkeypatch.setattr(
        "app.rag.matcher.assess_evidence_relevance_batch", fake_batch
    )
    cache_path = tmp_path / "relevance_cache.json"
    retriever = type("R", (), {
        "retrieve_many": lambda *args, **kwargs: (
            [[{
                "text": "CBS delivered fleet vehicle tracking.",
                "source": "a.docx",
                "page": 1,
                "score": 0.9,
            }]],
            [{"query_embedding_seconds": 0.01, "retrieval_seconds": 0.001}],
        )
    })()
    matcher = RequirementEvidenceMatcher(
        retriever=retriever,
        cache_path=cache_path,
    )
    results = matcher.match_requirements(
        ["Provide fleet vehicle tracking."], top_k=1
    )
    assert results[0]["status"] == "NOT_FOUND"
    assert results[0]["evidence"] == []
    assert matcher._cache == {}


def test_batch_malformed_json_falls_back_sequentially(monkeypatch):
    sequential = []

    def fake_generate(prompt, **kwargs):
        label = kwargs.get("label")
        if label == "assess_evidence_relevance_batch":
            return "not-json {"
        raise AssertionError(f"unexpected generate label: {label}")

    def fake_single(requirement, evidence_text):
        sequential.append((requirement, evidence_text))
        return {"relevant": False, "reason": f"sequential:{requirement[:8]}"}

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(ollama_client, "assess_evidence_relevance", fake_single)

    results = ollama_client.assess_evidence_relevance_batch([
        ("Provide fleet tracking.", "fleet evidence"),
        ("Provide SSO.", "sso evidence"),
    ])

    assert len(sequential) == 2
    assert all(item["relevant"] is False for item in results)
    assert results[0]["reason"].startswith("sequential:")


def test_batch_missing_pair_indexes_with_count_mismatch_falls_back(monkeypatch):
    sequential = []

    def fake_generate(prompt, **kwargs):
        # No ids and wrong length → cannot safely map by position.
        return json.dumps({
            "assessments": [
                {"relevant": True, "reason": "only one"},
            ]
        })

    def fake_single(requirement, evidence_text):
        sequential.append(requirement)
        return {"relevant": True, "reason": "fallback"}

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(ollama_client, "assess_evidence_relevance", fake_single)

    results = ollama_client.assess_evidence_relevance_batch([
        ("Req one", "Evidence one"),
        ("Req two", "Evidence two"),
    ])

    assert sequential == ["Req one", "Req two"]
    assert results == [
        {"relevant": True, "reason": "fallback"},
        {"relevant": True, "reason": "fallback"},
    ]


def test_batch_duplicate_pair_index_keeps_first_valid(monkeypatch):
    """Invalid duplicate pair_index must not discard an earlier valid result."""
    sequential = []

    def fake_generate(prompt, **kwargs):
        return json.dumps({
            "assessments": [
                {"pair_index": 0, "relevant": True, "reason": "first valid"},
                {"pair_index": 0, "relevant": "bad", "reason": "invalid dup"},
                {"pair_index": 1, "relevant": False, "reason": "second"},
            ]
        })

    def fake_single(requirement, evidence_text):
        sequential.append(requirement)
        return {"relevant": False, "reason": "should not run"}

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(ollama_client, "assess_evidence_relevance", fake_single)

    results = ollama_client.assess_evidence_relevance_batch([
        ("Req 0", "Ev 0"),
        ("Req 1", "Ev 1"),
    ])

    assert sequential == []
    assert results[0] == {"relevant": True, "reason": "first valid"}
    assert results[1] == {"relevant": False, "reason": "second"}


def test_batch_partial_parse_reuses_valid_and_sequences_only_missing(monkeypatch):
    sequential = []

    def fake_generate(prompt, **kwargs):
        return json.dumps({
            "assessments": [
                {"pair_index": 0, "relevant": True, "reason": "kept from batch"},
                {"pair_index": 1, "relevant": "maybe", "reason": "invalid bool"},
                {"pair_index": 2, "relevant": False, "reason": "also kept"},
            ]
        })

    def fake_single(requirement, evidence_text):
        sequential.append(requirement)
        return {"relevant": False, "reason": "sequential only for missing"}

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(ollama_client, "assess_evidence_relevance", fake_single)

    results = ollama_client.assess_evidence_relevance_batch([
        ("Req 0", "Ev 0"),
        ("Req 1", "Ev 1"),
        ("Req 2", "Ev 2"),
    ])

    assert sequential == ["Req 1"]
    assert results[0] == {"relevant": True, "reason": "kept from batch"}
    assert results[1] == {
        "relevant": False,
        "reason": "sequential only for missing",
    }
    assert results[2] == {"relevant": False, "reason": "also kept"}


def test_batch_single_assessment_for_three_pairs_is_partial(monkeypatch, capsys):
    """Live smoke shape consistent with response_chars≈280–360 and reused=1.

    After string-bool / colliding-index fixes, ``partial`` with reused=1 still
    occurs when ``assessments`` contains fewer than ``prepared`` entries: the
    positional rescue requires ``len(assessments) == prepared_count``.
    """
    sequential = []

    def fake_generate(prompt, **kwargs):
        # One complete assessment object (~size class of the live 281–357 char
        # batch responses), not three.
        return json.dumps({
            "assessments": [
                {
                    "pair_index": 0,
                    "relevant": True,
                    "reason": (
                        "The evidence discusses the same vendor ownership "
                        "obligation for the customer application component "
                        "across the listed commercial options in the tender."
                    ),
                }
            ]
        })

    def fake_single(requirement, evidence_text):
        sequential.append(requirement)
        return {"relevant": False, "reason": f"sequential:{requirement}"}

    ollama_client.clear_batch_diagnostic_events()
    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(ollama_client, "assess_evidence_relevance", fake_single)

    results = ollama_client.assess_evidence_relevance_batch([
        ("R0", "E0"),
        ("R1", "E1"),
        ("R2", "E2"),
    ])

    assert len(sequential) == 2
    assert results[0] == {
        "relevant": True,
        "reason": (
            "The evidence discusses the same vendor ownership "
            "obligation for the customer application component "
            "across the listed commercial options in the tender."
        ),
    }
    assert results[1]["reason"].startswith("sequential:")
    assert results[2]["reason"].startswith("sequential:")
    captured = capsys.readouterr().out
    assert "assessment_count=1" in captured
    assert "reused=1" in captured
    assert "missing=2" in captured
    assert "OLLAMA_RELEVANCE_BATCH raw_response" in captured
    assert "OLLAMA_RELEVANCE_BATCH structure" in captured
    assert "OLLAMA_RELEVANCE_BATCH diagnostic" in captured
    events = ollama_client.get_batch_diagnostic_events()
    assert len(events) == 1
    assert events[0]["map_status"] == "partial"
    assert events[0]["assessment_count"] == 1
    assert events[0]["parseable_items"] == 1
    assert events[0]["reused"] == 1
    assert events[0]["missing"] == 2
    assert events[0]["response_chars"] > 0
    assert events[0]["timed_out"] is False


def test_batch_diagnostic_records_ok_and_done_reason_from_generate(monkeypatch, capsys):
    """Leaf success path records map_status=ok plus generate done_reason."""
    response_body = {
        "assessments": [
            {"pair_index": 0, "relevant": True, "reason": "yes"},
            {"pair_index": 1, "relevant": False, "reason": "no"},
        ]
    }
    response_text = json.dumps(response_body)

    def fake_generate(prompt, **kwargs):
        ollama_client._log_generate_metrics(
            kwargs.get("label") or "generate",
            0.01,
            {"response": response_text, "done_reason": "stop", "eval_count": 12},
            len(prompt or ""),
            kwargs.get("num_predict") or 512,
        )
        return response_text

    ollama_client.clear_batch_diagnostic_events()
    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(
        ollama_client,
        "assess_evidence_relevance",
        lambda *_: (_ for _ in ()).throw(AssertionError("no sequential")),
    )

    results = ollama_client.assess_evidence_relevance_batch([
        ("R0", "E0"),
        ("R1", "E1"),
    ])

    assert results[0]["relevant"] is True
    assert results[1]["relevant"] is False
    events = ollama_client.get_batch_diagnostic_events()
    assert len(events) == 1
    assert events[0]["map_status"] == "ok"
    assert events[0]["assessment_count"] == 2
    assert events[0]["parseable_items"] == 2
    assert events[0]["reused"] == 2
    assert events[0]["missing"] == 0
    assert events[0]["response_chars"] == len(response_text)
    assert events[0]["done_reason"] == "stop"
    assert "OLLAMA_RELEVANCE_BATCH diagnostic" in capsys.readouterr().out


def test_batch_split_records_one_diagnostic_per_leaf(monkeypatch):
    """MAX_PAIRS split parents do not emit diagnostics; each leaf generate does."""
    def fake_generate(prompt, **kwargs):
        size = sum(
            1 for line in prompt.splitlines() if line.startswith("PAIR ")
        )
        return json.dumps({
            "assessments": [
                {"pair_index": i, "relevant": False, "reason": "split"}
                for i in range(size)
            ]
        })

    ollama_client.clear_batch_diagnostic_events()
    monkeypatch.setenv("OLLAMA_RELEVANCE_BATCH_MAX_PAIRS", "2")
    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(
        ollama_client,
        "assess_evidence_relevance",
        lambda *_: (_ for _ in ()).throw(AssertionError("no sequential")),
    )

    ollama_client.assess_evidence_relevance_batch([
        ("R1", "E1"),
        ("R2", "E2"),
        ("R3", "E3"),
        ("R4", "E4"),
    ])

    events = ollama_client.get_batch_diagnostic_events()
    # 4 pairs → 2+2 leaf generates; split parent emits none.
    assert len(events) == 2
    assert all(event["map_status"] == "ok" for event in events)
    assert [event["prepared"] for event in events] == [2, 2]


def test_batch_three_items_two_unparseable_relevant_is_partial(monkeypatch, capsys):
    """Alternate code path to reused=1: three rows, only one coercible relevant."""
    sequential = []

    def fake_generate(prompt, **kwargs):
        return json.dumps({
            "assessments": [
                {"pair_index": 0, "relevant": True, "reason": "ok"},
                {"pair_index": 1, "relevant": None, "reason": "null relevant"},
                {"pair_index": 2, "relevant": {"flag": True}, "reason": "object"},
            ]
        })

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(
        ollama_client,
        "assess_evidence_relevance",
        lambda req, ev: sequential.append(req) or {
            "relevant": False,
            "reason": f"sequential:{req}",
        },
    )

    results = ollama_client.assess_evidence_relevance_batch([
        ("R0", "E0"),
        ("R1", "E1"),
        ("R2", "E2"),
    ])

    assert sequential == ["R1", "R2"]
    assert results[0]["relevant"] is True
    captured = capsys.readouterr().out
    assert "assessment_count=3" in captured
    assert "reused=1" in captured
    assert "parseable_items=1" in captured


def test_map_batch_assessments_len_one_cannot_rescue_missing_slots():
    slots, status = ollama_client._map_batch_assessments(
        [{"pair_index": 0, "relevant": True, "reason": "only one"}],
        prepared_count=3,
    )
    assert status == "partial"
    assert slots[0] == {"relevant": True, "reason": "only one"}
    assert slots[1] is None
    assert slots[2] is None


def test_batch_reuses_all_when_relevant_are_strings(monkeypatch):
    """Measured smoke failure mode: Qwen often returns \"true\"/\"false\" strings."""
    sequential = []

    def fake_generate(prompt, **kwargs):
        return json.dumps({
            "assessments": [
                {"pair_index": 0, "relevant": "true", "reason": "a"},
                {"pair_index": 1, "relevant": "false", "reason": "b"},
                {"pair_index": 2, "relevant": "true", "reason": "c"},
            ]
        })

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(
        ollama_client,
        "assess_evidence_relevance",
        lambda *args: sequential.append(args) or {"relevant": False, "reason": "x"},
    )

    results = ollama_client.assess_evidence_relevance_batch([
        ("R0", "E0"),
        ("R1", "E1"),
        ("R2", "E2"),
    ])

    assert sequential == []
    assert results == [
        {"relevant": True, "reason": "a"},
        {"relevant": False, "reason": "b"},
        {"relevant": True, "reason": "c"},
    ]


def test_batch_colliding_pair_index_zero_rescued_by_position(monkeypatch):
    """Measured pattern: three-pair batch reused only one when indexes collide."""
    sequential = []

    def fake_generate(prompt, **kwargs):
        return json.dumps({
            "assessments": [
                {"pair_index": 0, "relevant": True, "reason": "first"},
                {"pair_index": 0, "relevant": False, "reason": "second"},
                {"pair_index": 0, "relevant": True, "reason": "third"},
            ]
        })

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(
        ollama_client,
        "assess_evidence_relevance",
        lambda *args: sequential.append(args) or {"relevant": False, "reason": "x"},
    )

    results = ollama_client.assess_evidence_relevance_batch([
        ("R0", "E0"),
        ("R1", "E1"),
        ("R2", "E2"),
    ])

    assert sequential == []
    assert results[0]["reason"] == "first"
    assert results[1]["reason"] == "second"
    assert results[2]["reason"] == "third"


def test_batch_one_based_pair_index_values_map_correctly(monkeypatch):
    def fake_generate(prompt, **kwargs):
        return json.dumps({
            "assessments": [
                {"pair_index": 1, "relevant": True, "reason": "one"},
                {"pair_index": 2, "relevant": False, "reason": "two"},
                {"pair_index": 3, "relevant": True, "reason": "three"},
            ]
        })

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(
        ollama_client,
        "assess_evidence_relevance",
        lambda *_: (_ for _ in ()).throw(AssertionError("no sequential")),
    )
    results = ollama_client.assess_evidence_relevance_batch([
        ("R0", "E0"),
        ("R1", "E1"),
        ("R2", "E2"),
    ])
    assert [item["reason"] for item in results] == ["one", "two", "three"]


def test_batch_wrapped_json_still_parses(monkeypatch):
    def fake_generate(prompt, **kwargs):
        return (
            "Here is the result:\n"
            '{"assessments":[{"pair_index":0,"relevant":false,"reason":"no"},'
            '{"pair_index":1,"relevant":true,"reason":"yes"}]}\n'
            "Thanks."
        )

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    monkeypatch.setattr(
        ollama_client,
        "assess_evidence_relevance",
        lambda *_: (_ for _ in ()).throw(AssertionError("no fallback")),
    )
    results = ollama_client.assess_evidence_relevance_batch([
        ("A", "a"),
        ("B", "b"),
    ])
    assert results[0]["relevant"] is False
    assert results[1]["relevant"] is True


def test_batch_empty_evidence_skips_llm_for_that_slot(monkeypatch):
    def fake_generate(prompt, **kwargs):
        assert "PAIR 0:" in prompt
        assert "PAIR 1:" not in prompt
        return json.dumps({
            "assessments": [
                {"pair_index": 0, "relevant": True, "reason": "ok"},
            ]
        })

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    results = ollama_client.assess_evidence_relevance_batch([
        ("Req", "Evidence text"),
        ("Req empty", "   "),
    ])
    assert results[0]["relevant"] is True
    assert results[1] == {
        "relevant": False,
        "reason": "Evidence text is empty.",
    }


def test_match_requirements_logs_and_uses_batch_helper(monkeypatch, tmp_path, capsys):
    batch_calls = []

    def fake_batch(pairs):
        batch_calls.append(list(pairs))
        return [{"relevant": True, "reason": "batched"} for _ in pairs]

    monkeypatch.setattr(
        "app.rag.matcher.assess_evidence_relevance_batch", fake_batch
    )
    retriever = type("R", (), {
        "retrieve_many": lambda *args, **kwargs: (
            [
                [{
                    "text": "CBS delivered fleet vehicle tracking.",
                    "source": "a.docx",
                    "page": 1,
                    "score": 0.9,
                }],
                [{
                    "text": "CBS delivered SSO.",
                    "source": "b.docx",
                    "page": 2,
                    "score": 0.91,
                }],
            ],
            [
                {"query_embedding_seconds": 0.01, "retrieval_seconds": 0.001},
                {"query_embedding_seconds": 0.01, "retrieval_seconds": 0.001},
            ],
        )
    })()
    matcher = RequirementEvidenceMatcher(
        retriever=retriever,
        cache_path=tmp_path / "cache.json",
    )
    results = matcher.match_requirements([
        "Provide fleet vehicle tracking.",
        "Provide SSO.",
    ], top_k=1)

    captured = capsys.readouterr().out
    assert "assess_evidence_relevance_batch" in captured
    assert "uncached_pairs=2" in captured
    assert len(batch_calls) == 1
    assert len(batch_calls[0]) == 2
    assert all(item["status"] == "REVIEW_REQUIRED" for item in results)


def test_one_based_pair_ids_map_correctly(monkeypatch):
    def fake_generate(prompt, **kwargs):
        return json.dumps({
            "assessments": [
                {"id": 1, "relevant": True, "reason": "first"},
                {"id": 2, "relevant": False, "reason": "second"},
            ]
        })

    monkeypatch.setattr(ollama_client, "generate_response", fake_generate)
    results = ollama_client.assess_evidence_relevance_batch([
        ("Req A", "Ev A"),
        ("Req B", "Ev B"),
    ])
    assert results[0]["reason"] == "first"
    assert results[1]["reason"] == "second"
