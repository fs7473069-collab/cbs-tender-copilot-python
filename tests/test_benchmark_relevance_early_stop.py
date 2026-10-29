"""Offline guarantees for diagnostics/benchmark_relevance_early_stop.py."""

from diagnostics.benchmark_relevance_early_stop import run_benchmark


def test_early_stop_benchmark_is_offline_and_counts_mocks(tmp_path):
    report = run_benchmark(cache_dir=tmp_path)

    assert report["mocks_used"] is True
    assert report["network_guard_trips"] == 0
    assert report["baseline_kind"] == "simulated"

    baseline = report["simulated_baseline"]
    current = report["current_implementation"]

    assert baseline["single_assess_calls"] > 0
    assert baseline["batch_invocations"] == 0
    assert baseline["evidence_counts"] == [2, 2]

    assert current["batch_invocations"] > 0
    assert current["batch_pair_assessments"] > 0
    assert current["single_assess_calls"] == 0
    assert current["batch_pair_assessments"] == 1
    assert current["evidence_counts"] == [1, 1]

    assert baseline["single_assess_calls"] > current["batch_pair_assessments"]
    assert "omit additional useful supporting evidence" in report[
        "max_accepted_evidence_note"
    ]
