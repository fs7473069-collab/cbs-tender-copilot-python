"""Text hygiene for export cells/paragraphs (synthetic data only)."""

from io import BytesIO

import pytest
from docx import Document
from openpyxl import load_workbook

from app.export import render_excel, render_word
from app.export.text import (
    EXCEL_CELL_MAX_UTF16_UNITS,
    TRUNCATION_MARKER,
    excel_text,
    strip_illegal_xml,
    to_text,
    truncate_utf16,
    utf16_length,
)
from tests.export_fixtures import make_snapshot


# Formula injection --------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "=1+1", "+1+1", "-1+1", "@SUM(A1)",
    "  =1+1", "\n=1+1", "\n\n  -cmd", " \t+1", "\u00a0=1", "\u3000@x",
    "\u2003=1", "\u200b=1", "\ufeff=1",
    "\uff1dSUM(A1)", "\uff0b1", "\uff0d1", "\uff20SUM(A1)",
    "\ufe66 1", "\ufe62 1", "\ufe63 1",
    "-cmd|' /C calc'!A0", "=cmd|' /C calc'!A0", "-", "--5", "-5-", "-5 ",
    " -5", "-1,23", "-1e5", "-0x10", "-\u0665",  # Arabic-Indic digit
    "\tcmd", "\rcmd", "\t-5", "\r5",
])
def test_formula_triggers_are_neutralised(payload):
    assert excel_text(payload) == "'" + payload


@pytest.mark.parametrize("value", [
    "-5", "-3.14", "-1,234.50", "+7", "-0", "-.5", "+1,000", "5", "12.0",
    "plain text", "", "a=b", "email@example.test", "1-2",
])
def test_plain_values_and_numbers_keep_no_apostrophe(value):
    assert excel_text(value) == value


@pytest.mark.parametrize("value, expected", [
    (-5, "-5"), (-3.14, "-3.14"), (0.9, "0.9"), (-1e-05, "-1e-05"),
    (12, "12"), (True, "Yes"), (False, "No"),
    (float("-inf"), "'-inf"), (float("inf"), "inf"), (float("nan"), "nan"),
])
def test_real_numbers_are_not_mangled(value, expected):
    assert excel_text(value) == expected


def test_numeric_strings_are_written_as_text_cells():
    snapshot = make_snapshot()
    snapshot["requirements"][0]["content"] = "-1,234.50"
    snapshot["requirements"][0]["confidence"] = -0.5
    workbook = load_workbook(BytesIO(render_excel(snapshot)))
    sheet = workbook["Requirements"]
    headers = [c.value for c in sheet[1]]
    content = sheet.cell(row=2, column=headers.index("Requirement") + 1)
    confidence = sheet.cell(row=2, column=headers.index("Confidence") + 1)
    assert (content.value, content.data_type) == ("-1,234.50", "s")
    assert (confidence.value, confidence.data_type) == ("-0.5", "s")


# Invalid XML characters ------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "\x00", "\x08", "\x0b", "\x0c", "\x0e", "\x1f", "\ufffe", "\uffff",
    "\ud800", "\udbff", "\udc00", "\udfff",
])
def test_invalid_xml_characters_are_removed(bad):
    assert strip_illegal_xml(f"a{bad}b") == "ab"
    assert to_text(f"a{bad}b") == "ab"


def test_allowed_whitespace_and_non_bmp_are_kept():
    text = "tab\there\nline\rcr \u00e9 \U0001F600 \ufffd \ue000"
    assert strip_illegal_xml(text) == text


def test_split_surrogate_pair_is_recombined_not_dropped():
    assert strip_illegal_xml("x\ud83d\ude00y") == "x\U0001F600y"
    assert strip_illegal_xml("x\ude00\ud83dy") == "xy"


def test_json_values_are_cleaned_too():
    assert to_text({"k": "a\ufffeb"}) == '{"k": "ab"}'


def test_renderers_accept_every_invalid_character():
    # Low surrogate before high: two lone surrogates, not a valid pair.
    bad = "".join(["\x00", "\x0b", "\ufffe", "\uffff", "\udfff", "\ud800"])
    snapshot = make_snapshot()
    snapshot["tender"]["title"] = f"Title{bad}"
    snapshot["requirements"][0]["content"] = f"Req{bad}uirement"
    snapshot["actions"][0]["title"] = f"=Act{bad}"
    snapshot["draftSections"][0]["title"] = f"Sec{bad}"
    snapshot["draftSections"][0]["content"] = f"Para{bad}graph"

    workbook = load_workbook(BytesIO(render_excel(snapshot)))
    values = [c.value for c in workbook["Requirements"][2]]
    assert "Requirement" in values
    assert workbook["Actions"]["C2"].value == "'=Act"
    text = [p.text for p in Document(BytesIO(render_word(snapshot))).paragraphs]
    assert text[0] == "Title"
    assert "Sec" in text and "Paragraph" in text


# Excel cell length (UTF-16 code units) ----------------------------------------------

def test_utf16_length_counts_non_bmp_as_two():
    assert utf16_length("a\U0001F600") == 3


def test_text_at_limit_is_unchanged():
    text = "a" * EXCEL_CELL_MAX_UTF16_UNITS
    assert excel_text(text) == text


def test_emoji_text_is_truncated_by_utf16_units_with_marker():
    text = "\U0001F600" * 20000  # 20,000 code points = 40,000 UTF-16 units
    result = excel_text(text)
    assert result.endswith(TRUNCATION_MARKER)
    assert utf16_length(result) <= EXCEL_CELL_MAX_UTF16_UNITS
    body = result[: -len(TRUNCATION_MARKER)]
    assert set(body) == {"\U0001F600"}
    result.encode("utf-8")


def test_truncation_never_splits_a_surrogate_pair():
    budget = EXCEL_CELL_MAX_UTF16_UNITS - utf16_length(TRUNCATION_MARKER)
    text = "a" * (budget - 1) + "\U0001F600" * 10
    result = truncate_utf16(text)
    assert result == "a" * (budget - 1) + TRUNCATION_MARKER
    result.encode("utf-8")


def test_one_unit_over_the_limit_is_truncated():
    text = "a" * (EXCEL_CELL_MAX_UTF16_UNITS - 1) + "\U0001F600"
    result = excel_text(text)
    assert result.endswith(TRUNCATION_MARKER)
    assert utf16_length(result) == EXCEL_CELL_MAX_UTF16_UNITS


def test_rendered_long_emoji_cell_fits_excel_limit():
    snapshot = make_snapshot()
    snapshot["requirements"][0]["content"] = "\U0001F600" * 20000
    workbook = load_workbook(BytesIO(render_excel(snapshot)))
    value = workbook["Requirements"]["C2"].value
    assert value.endswith(TRUNCATION_MARKER)
    assert utf16_length(value) <= EXCEL_CELL_MAX_UTF16_UNITS


def test_neutralised_long_value_still_fits():
    result = excel_text("=" + "x" * 40000)
    assert result.startswith("'=")
    assert utf16_length(result) == EXCEL_CELL_MAX_UTF16_UNITS
