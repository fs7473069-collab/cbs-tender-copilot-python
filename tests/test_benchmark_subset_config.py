"""Offline tests for diagnostics/benchmark_subset_extract_match selection."""

import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "diagnostics" / "benchmark_subset_extract_match.py"


def _load_benchmark_module():
    spec = importlib.util.spec_from_file_location(
        "benchmark_subset_extract_match", SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_parse_selection_default_is_full():
    mod = _load_benchmark_module()
    assert mod.parse_selection([]) == mod.SELECTION_FULL


def test_parse_selection_smoke_ownership_flag():
    mod = _load_benchmark_module()
    assert mod.parse_selection(["--smoke-ownership"]) == (
        mod.SELECTION_SMOKE_OWNERSHIP
    )


def test_load_inputs_full_is_exactly_three():
    mod = _load_benchmark_module()
    inputs = mod._load_inputs(selection=mod.SELECTION_FULL)
    assert len(inputs) == mod.MAX_CHUNKS == 3
    assert inputs[0]["id"] == "ownership_option_abc"
    assert inputs[1]["id"] == "ev_tender_chunk_0"
    assert inputs[2]["id"] == "ev_tender_chunk_9"


def test_load_inputs_smoke_ownership_is_single_fixture():
    mod = _load_benchmark_module()
    inputs = mod._load_inputs(selection=mod.SELECTION_SMOKE_OWNERSHIP)
    assert len(inputs) == 1
    assert len(inputs) <= mod.MAX_CHUNKS
    assert inputs[0]["id"] == "ownership_option_abc"
    assert "ownership_option_abc_chunk.json" in inputs[0]["source"].replace(
        "\\", "/"
    )


def test_load_inputs_rejects_unknown_selection():
    mod = _load_benchmark_module()
    with pytest.raises(ValueError, match="Unknown selection"):
        mod._load_inputs(selection="all_48_chunks")


def test_should_record_batch_fallback_skips_split_parent():
    mod = _load_benchmark_module()
    # Outer 7-pair call with max_pairs=3 must not record (children do).
    assert mod.should_record_batch_fallback([0] * 7, fallback_calls=5, max_pairs=3) is False
    assert mod.should_record_batch_fallback([0] * 3, fallback_calls=2, max_pairs=3) is True
    assert mod.should_record_batch_fallback([0], fallback_calls=1, max_pairs=3) is False
    assert mod.should_record_batch_fallback([0] * 3, fallback_calls=0, max_pairs=3) is False
    # max_pairs=0 disables splitting; any multi-pair leaf may record.
    assert mod.should_record_batch_fallback([0] * 7, fallback_calls=5, max_pairs=0) is True


def test_record_batch_fallback_counts_each_sequential_once():
    """Smoke shape: 7 pairs → split 3+3+1 with two partial leaves (2+2).

    Outer wrapper must not add a third event; total sequential = 4.
    """
    mod = _load_benchmark_module()
    counters = {
        "batch_fallbacks": [],
        "sequential_relevance_from_batch_fallback": 0,
    }
    max_pairs = 3

    # Outer split parent sees all nested sequentials but must not record.
    assert mod.record_batch_fallback_event(
        counters, [0] * 7, fallback_calls=5, max_pairs=max_pairs
    ) is False
    # First leaf partial.
    assert mod.record_batch_fallback_event(
        counters, [0] * 3, fallback_calls=2, max_pairs=max_pairs
    ) is True
    # Second leaf partial.
    assert mod.record_batch_fallback_event(
        counters, [0] * 3, fallback_calls=2, max_pairs=max_pairs
    ) is True
    # Remainder single-pair short-circuit is not a fallback.
    assert mod.record_batch_fallback_event(
        counters, [0], fallback_calls=1, max_pairs=max_pairs
    ) is False

    assert counters["sequential_relevance_from_batch_fallback"] == 4
    assert counters["batch_fallbacks"] == [
        {"input_pairs": 3, "sequential_generate_calls": 2},
        {"input_pairs": 3, "sequential_generate_calls": 2},
    ]
