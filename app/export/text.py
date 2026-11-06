"""Cell/paragraph text hygiene shared by the Excel and Word renderers."""

import json
import math
import re

# Characters outside the XML 1.0 Char production. openpyxl and python-docx
# (lxml) both refuse them: C0 controls except tab/LF/CR, lone UTF-16
# surrogates and the non-characters U+FFFE / U+FFFF.
INVALID_XML_CHARACTERS_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]"
)

# Leading characters that spreadsheet apps interpret as a formula or command,
# including the full-width and small-form variants.
FORMULA_TRIGGERS = frozenset("=+-@\uff1d\uff0b\uff0d\uff20\ufe66\ufe62\ufe63")
# Leading characters that always trigger neutralisation, even when followed
# by ordinary text.
FORMULA_CONTROL_PREFIXES = ("\t", "\r")
# Invisible characters skipped (like whitespace) when finding the first
# significant character.
_INVISIBLE_LEADING = frozenset("\u200b\u200c\u200d\u2060\ufeff")
# Plain decimal numbers (ASCII digits only), e.g. -5, -3.14, -1,234.50, +.5.
# Anything else starting with a trigger (e.g. "-1+1") is neutralised.
PLAIN_NUMBER_RE = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|\.[0-9]+)"
)

# Excel's per-cell limit counts UTF-16 code units, so a non-BMP character
# (e.g. an emoji) uses two of them.
EXCEL_CELL_MAX_UTF16_UNITS = 32767
EXCEL_CELL_MAX_CHARS = EXCEL_CELL_MAX_UTF16_UNITS
TRUNCATION_MARKER = " [truncated]"


def strip_illegal_xml(text):
    """Remove characters that cannot appear in XLSX/DOCX XML.

    Valid surrogate pairs (e.g. an emoji split into two code points) are
    first recombined so that only lone surrogates are removed.
    """
    if any("\ud800" <= ch <= "\udfff" for ch in text):
        text = text.encode("utf-16-le", "surrogatepass").decode(
            "utf-16-le", "surrogatepass"
        )
    return INVALID_XML_CHARACTERS_RE.sub("", text)


def to_text(value):
    """Plain-text form of a snapshot value; None becomes an empty string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, str):
        return strip_illegal_xml(value)
    if isinstance(value, (int, float)):
        return str(value)
    return strip_illegal_xml(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _first_significant_char(text):
    for ch in text:
        if not ch.isspace() and ch not in _INVISIBLE_LEADING:
            return ch
    return None


def needs_formula_neutralisation(text):
    if text.startswith(FORMULA_CONTROL_PREFIXES):
        return True
    if _first_significant_char(text) not in FORMULA_TRIGGERS:
        return False
    return PLAIN_NUMBER_RE.fullmatch(text) is None


def neutralize_formula(text):
    if needs_formula_neutralisation(text):
        return "'" + text
    return text


def utf16_length(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def truncate_utf16(text, max_units=EXCEL_CELL_MAX_UTF16_UNITS):
    """Fit ``text`` in ``max_units`` UTF-16 units, marking any truncation.

    Never splits a surrogate pair.
    """
    if utf16_length(text) <= max_units:
        return text
    budget = max_units - utf16_length(TRUNCATION_MARKER)
    encoded = text.encode("utf-16-le", "surrogatepass")[: budget * 2]
    if len(encoded) >= 2 and 0xD8 <= encoded[-1] <= 0xDB:
        encoded = encoded[:-2]
    return encoded.decode("utf-16-le", "surrogatepass") + TRUNCATION_MARKER


def excel_text(value):
    """Cell text: XML-safe, formula-neutralised and within Excel's limit.

    Finite real numbers keep their plain form (they cannot be formulas).
    """
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    ):
        text = str(value)
    else:
        text = neutralize_formula(to_text(value))
    return truncate_utf16(text)
