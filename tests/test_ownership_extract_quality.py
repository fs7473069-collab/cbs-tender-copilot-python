"""Offline tests for strict Pricing & product engine ownership diagnostics."""

import json
from pathlib import Path

import pytest

from diagnostics.ownership_extract_quality import check_pricing_engine_ownership


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    PROJECT_ROOT / "diagnostics" / "fixtures" / "ownership_option_abc_chunk.json"
)
BASELINE_PATH = (
    PROJECT_ROOT / "diagnostics" / "extract_ownership_chunk_baseline.json"
)
TWO_RUN_PATH = (
    PROJECT_ROOT / "diagnostics" / "extract_ownership_chunk_two_run.json"
)


def test_extract_prompt_requires_preserving_every_option_column(monkeypatch):
    """Controlled prompt experiment: matrix rows must keep all option assignments."""
    from app.llm.ollama_client import (
        EXTRACT_NUM_PREDICT,
        extract_num_predict_for_text,
        extract_requirements,
    )

    captured = {}

    def fake_generate(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["num_predict"] = kwargs.get("num_predict")
        return json.dumps({"requirements": []})

    monkeypatch.setattr("app.llm.ollama_client.generate_response", fake_generate)
    text = "Priority: M The vendor shall provide SSO for administrators."
    extract_requirements({"text": text})
    prompt = captured["prompt"]
    assert "every option present in that row" in prompt.casefold()
    assert "do not stop after summarizing only some columns" in prompt.casefold()
    assert EXTRACT_NUM_PREDICT == 1536
    assert captured["num_predict"] == extract_num_predict_for_text(text)
    assert captured["num_predict"] <= EXTRACT_NUM_PREDICT


def test_fixture_pricing_row_contains_all_three_option_assignments():
    """Source chunk text must include Pricing A/B/C ownership before any LLM call."""
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    text = fixture["text"]
    # Isolate the Pricing & product engine row (through next Component or EOS).
    start = text.index("Component: Pricing & product engine")
    rest = text[start:]
    end_markers = [
        rest.find("\nComponent:", 1),
        rest.find("\r\nComponent:", 1),
    ]
    end = min((i for i in end_markers if i != -1), default=len(rest))
    pricing_row = rest[:end]
    normalized = " ".join(pricing_row.split())

    assert "Option A (Core): Qualiteq" in normalized
    assert "Option B (+ Pricing Engine): Vendor" in normalized
    assert "Option C (Full Stack): Vendor" in normalized
    # Ensure Option C is on the Pricing row, not only elsewhere in the chunk.
    assert normalized.index("Option A (Core): Qualiteq") < normalized.index(
        "Option C (Full Stack): Vendor"
    )


def test_pricing_check_passes_on_complete_option_abc_ownership():
    requirements = [{
        "text": (
            "The Pricing & product engine will be provided by Qualiteq for "
            "Option A (Core), by the Vendor for Option B (+ Pricing Engine), "
            "and by the Vendor for Option C (Full Stack)."
        ),
        "category": "Functional",
        "mandatory": False,
        "confidence": "High",
    }]
    result = check_pricing_engine_ownership(requirements)
    assert result["passed"] is True
    assert result["missing_assignments"] == []
    assert result["assignments"]["option_a_qualiteq"]["present"] is True
    assert result["assignments"]["option_b_vendor"]["present"] is True
    assert result["assignments"]["option_c_vendor"]["present"] is True


def test_pricing_check_detects_known_option_c_omission():
    # Known failure shape from saved ownership-chunk extracts.
    requirements = [{
        "text": (
            "The Pricing & product engine will be provided by Qualiteq for "
            "Option A (Core) and by the vendor for Option B (+ Pricing Engine)."
        ),
        "category": "Functional",
        "mandatory": False,
        "confidence": "High",
    }]
    result = check_pricing_engine_ownership(requirements)
    assert result["passed"] is False
    assert result["missing_assignments"] == ["Option C (Full Stack): Vendor"]
    assert result["assignments"]["option_a_qualiteq"]["present"] is True
    assert result["assignments"]["option_b_vendor"]["present"] is True
    assert result["assignments"]["option_c_vendor"]["present"] is False
    assert "Option C (Full Stack): Vendor" in (result.get("note") or "")


def test_pricing_check_ignores_generic_all_options_and_other_rows():
    # Heuristic A/B/C / "all options" flags would pass; Pricing row is absent.
    requirements = [
        {
            "text": (
                "The Customer app (mobile + web) UI will be provided by the "
                "vendor for all options."
            ),
            "mandatory": False,
        },
        {
            "text": (
                "The OCPP / charger middleware will be provided by Qualiteq "
                "for Options A (Core) and B (+ Pricing Engine), and by the "
                "vendor (optional) for Option C (Full Stack)."
            ),
            "mandatory": False,
        },
    ]
    result = check_pricing_engine_ownership(requirements)
    assert result["passed"] is False
    assert result["matched_requirement_text"] is None
    assert result["missing_assignments"] == [
        "Option A: Qualiteq",
        "Option B: Vendor",
        "Option C (Full Stack): Vendor",
    ]


@pytest.mark.skipif(
    not BASELINE_PATH.is_file(),
    reason="local-only asset absent: diagnostics/extract_ownership_chunk_baseline.json",
)
def test_historical_baseline_fails_pricing_option_c_check():
    """Older CPU baseline extract omitted Pricing Option C; validator must catch it."""
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    baseline_check = check_pricing_engine_ownership(baseline["requirements"])
    assert baseline_check["passed"] is False
    assert baseline_check["missing_assignments"] == [
        "Option C (Full Stack): Vendor"
    ]
    assert baseline_check["assignments"]["option_a_qualiteq"]["present"] is True
    assert baseline_check["assignments"]["option_b_vendor"]["present"] is True
    assert baseline_check["assignments"]["option_c_vendor"]["present"] is False


@pytest.mark.skipif(
    not TWO_RUN_PATH.is_file(),
    reason="local-only asset absent: diagnostics/extract_ownership_chunk_two_run.json",
)
def test_latest_two_run_passes_pricing_option_abc_check():
    """Latest live two-run extracts preserve Pricing A/B/C ownership."""
    two_run = json.loads(TWO_RUN_PATH.read_text(encoding="utf-8"))
    for run_key in ("run_1", "run_2"):
        check = check_pricing_engine_ownership(two_run[run_key]["requirements"])
        assert check["passed"] is True, run_key
        assert check["missing_assignments"] == [], run_key
        assert check["assignments"]["option_a_qualiteq"]["present"] is True, run_key
        assert check["assignments"]["option_b_vendor"]["present"] is True, run_key
        assert check["assignments"]["option_c_vendor"]["present"] is True, run_key
        text = (check.get("matched_requirement_text") or "").casefold()
        assert "option a" in text and "qualiteq" in text, run_key
        assert "option b" in text and "vendor" in text, run_key
        assert (
            "option c" in text or "full stack" in text
        ) and "vendor" in text, run_key
