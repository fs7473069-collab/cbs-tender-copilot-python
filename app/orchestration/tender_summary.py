"""Tender cover details (title, customer, reference, deadline, summary).

Values come from the inbound request when supplied, otherwise from the tender
text: deterministic patterns first, then at most one bounded LLM call over the
opening pages. Every document-derived value must be supported by the source
text; anything not established stays None.
"""

import hashlib
import json
import os
import re
from datetime import date, datetime

from app.document.docx_processor import extract_docx_text
from app.document.pdf_processor import extract_pdf_text
from app.llm import ollama_client
from app.response.response_builder import REQUEST_TENDER_FIELDS


SUMMARY_DIR = os.path.join("chunks", "requirements", "summary")
SUMMARY_FIELDS = ollama_client.TENDER_SUMMARY_FIELDS
SUMMARY_PAGES = 3
SUMMARY_MAX_CHARS = 6000
SUMMARY_MAX_LENGTH = 600

_MONTHS = {
    name: index
    for index, names in enumerate((
        ("jan", "january"), ("feb", "february"), ("mar", "march"),
        ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
        ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"),
        ("nov", "november"), ("dec", "december"),
    ), start=1)
    for name in names
}
_MONTH = r"(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DATE_PATTERNS = (
    re.compile(rf"(?i)\b{_MONTH}\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<year>20\d\d)\b"),
    re.compile(rf"(?i)\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH},?\s+(?P<year>20\d\d)\b"),
    re.compile(r"\b(?P<first>\d{1,2})[-/.](?P<second>\d{1,2})[-/.](?P<year>20\d\d)\b"),
)
_TIME_PATTERNS = (
    re.compile(r"(?i)\b(?P<hour>\d{1,2})[:.](?P<minute>\d{2})\s*(?P<ampm>[ap]\.?m\.?)?"),
    re.compile(r"(?i)\b(?P<hhmm>\d{3,4})\s*(?:hours|hrs)\b"),
    re.compile(r"(?i)\b(?P<hour>\d{1,2})\s*(?P<ampm>[ap]\.?m\.?)"),
)
_DEADLINE_ANCHOR = re.compile(
    r"(?i)(deadline\s+for\s+(?:the\s+)?submission|submission\s+deadline|"
    r"last\s+date\s+(?:for|of)\s+(?:the\s+)?(?:submission|receipt)|closing\s+date)"
)
# Opening dates/times often follow the deadline clause; never take those.
_OPENING_WORD = re.compile(r"(?i)\bopen(?:ing|ed)?\b")
_DEADLINE_WINDOW = 400
_REFERENCE_RE = re.compile(
    r"(?im)^[^\S\n]*(?:tender|bid|rfp|rfq|rft|rfi)"
    r"(?:[^\S\n]+(?:notice|enquiry|inquiry|document))?[^\S\n]*"
    r"(?:ref(?:erence)?|no|number|#)\.?(?:[^\S\n]*(?:no\.?|#))?[^\S\n]*[:#.\-]?"
    r"[^\S\n]*(?P<value>[A-Za-z0-9(][^\n]{1,80}?)[^\S\n]*$"
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _squash(text):
    return re.sub(r"[\s\"'“”‘’]+", "", (text or "")).casefold()


def _tokens(text):
    return _TOKEN_RE.findall((text or "").casefold())


def _undo_letter_spacing(value):
    """Collapse ``2 0 2 5 - 2 6``-style spacing produced by PDF extraction."""
    tokens = value.split()
    if len(tokens) >= 4 and sum(len(t) == 1 for t in tokens) / len(tokens) >= 0.6:
        return "".join(tokens)
    return value


def find_tender_reference(text):
    for match in _REFERENCE_RE.finditer(text or ""):
        value = match.group("value").strip().rstrip(".,;:")
        if any(char.isdigit() for char in value):
            return _undo_letter_spacing(value)
    return None


def _parse_date(match):
    groups = match.groupdict()
    year = int(groups["year"])
    if groups.get("month"):
        month = _MONTHS.get(groups["month"].casefold().rstrip("."))
        day = int(groups["day"])
    else:
        first, second = int(groups["first"]), int(groups["second"])
        # Numeric dates are only accepted when day/month order is unambiguous.
        if first > 12 >= second:
            day, month = first, second
        elif second > 12 >= first:
            day, month = second, first
        else:
            return None
    try:
        return date(year, month, day)
    except (TypeError, ValueError):
        return None


def _parse_time(match):
    groups = match.groupdict()
    if groups.get("hhmm"):
        raw = groups["hhmm"].zfill(4)
        hour, minute = int(raw[:2]), int(raw[2:])
    else:
        hour, minute = int(groups["hour"]), int(groups.get("minute") or 0)
    ampm = (groups.get("ampm") or "").casefold().replace(".", "")
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _first_date(text):
    found = []
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            parsed = _parse_date(match)
            if parsed is not None:
                found.append((match.start(), match.end(), parsed))
    return min(found, default=None)


def _nearest_time(text, date_start, date_end):
    best = None
    for pattern in _TIME_PATTERNS:
        for match in pattern.finditer(text):
            if date_start <= match.start() < date_end:
                continue
            parsed = _parse_time(match)
            if parsed is None:
                continue
            distance = min(abs(match.end() - date_start), abs(match.start() - date_end))
            if distance <= 40 and (best is None or distance < best[0]):
                best = (distance, parsed)
    return best[1] if best else None


def _format_deadline(day, time_value):
    if time_value is None:
        return day.isoformat()
    return datetime(day.year, day.month, day.day, *time_value).isoformat()


def find_submission_deadline(text):
    """ISO date/time of the bid submission deadline, or None.

    Every deadline anchor must agree; conflicting candidates yield None. No
    time zone is added because tenders rarely state one explicitly.
    """
    flat = " ".join((text or "").split())
    candidates = set()
    for anchor in _DEADLINE_ANCHOR.finditer(flat):
        window = flat[anchor.end():anchor.end() + _DEADLINE_WINDOW]
        first = _first_date(window)
        if first is None:
            continue
        start, end, day = first
        if _OPENING_WORD.search(window[:start]):
            continue
        candidates.add(_format_deadline(day, _nearest_time(window, start, end)))
    if len(candidates) == 1:
        return candidates.pop()
    return None


def normalize_deadline_value(value):
    """ISO form of an explicit date string, or None when it is not parseable."""
    if not isinstance(value, str):
        return None
    first = _first_date(value)
    if first is None:
        return None
    start, end, day = first
    return _format_deadline(day, _nearest_time(value, start, end))


def is_supported(field, value, source_text):
    """True only when ``value`` is established by ``source_text``."""
    if not isinstance(value, str) or len(value.strip()) < 2:
        return False
    if _squash(value) in _squash(source_text):
        return True
    if field != "summary" or len(value) > SUMMARY_MAX_LENGTH:
        return False
    # A summary may be lightly re-phrased, but every word must come from the
    # source so no new facts are introduced.
    source_tokens = set(_tokens(source_text))
    value_tokens = _tokens(value)
    return bool(value_tokens) and all(token in source_tokens for token in value_tokens)


def verify_llm_values(values, source_text):
    """Drop unsupported LLM values; returns ``(verified, rejected_fields)``."""
    verified, rejected = {}, []
    for field in SUMMARY_FIELDS:
        value = (values or {}).get(field)
        if not isinstance(value, str) or not value.strip():
            verified[field] = None
            continue
        value = value.strip()
        if field == "submissionDeadline":
            supported = is_supported(field, value, source_text)
            normalized = normalize_deadline_value(value) if supported else None
            verified[field] = normalized
            if normalized is None:
                rejected.append(field)
        elif is_supported(field, value, source_text):
            verified[field] = (
                _undo_letter_spacing(value) if field == "tenderReference" else value
            )
        else:
            verified[field] = None
            rejected.append(field)
    return verified, rejected


def _load_pages(file_path):
    lowered = file_path.lower()
    if lowered.endswith(".pdf"):
        return extract_pdf_text(file_path)
    if lowered.endswith(".docx"):
        return extract_docx_text(file_path)
    raise ValueError("Unsupported file type. Supported formats are PDF and DOCX.")


def _opening_text(pages):
    texts = [
        page.get("text") or ""
        for page in pages[:SUMMARY_PAGES]
        if isinstance(page, dict)
    ]
    return "\n\n".join(text for text in texts if text.strip())[:SUMMARY_MAX_CHARS]


def request_values(request_tender):
    """Published-field view of the request ``tender`` object (None if absent)."""
    tender = request_tender if isinstance(request_tender, dict) else {}
    values = {}
    for field, request_key in REQUEST_TENDER_FIELDS.items():
        value = tender.get(request_key)
        if isinstance(value, str) and not value.strip():
            value = None
        values[field] = value
    return values


def _sidecar_path(document_path):
    path_hash = hashlib.sha256(document_path.encode("utf-8")).hexdigest()
    return os.path.join(SUMMARY_DIR, f"{path_hash}.json")


def _cache_key(opening_text):
    return hashlib.sha256(json.dumps({
        "text": opening_text,
        "prompt": ollama_client.TENDER_SUMMARY_PROMPT_VERSION,
        "model": ollama_client.MODEL_NAME,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _load_cached_llm(path, cache_key):
    try:
        with open(path, encoding="utf-8") as file:
            saved = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    if saved.get("cache_key") != cache_key or not isinstance(saved.get("llm_raw"), dict):
        return None
    return saved["llm_raw"]


def _save_sidecar(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
    os.replace(temporary, path)


def summarize_tender_document(file_path, request_tender=None, resume=True):
    """Document-derived cover details for fields the request does not supply.

    Returns a dict keyed by published result field; values are strings or
    None. Request-supplied fields are returned as None here because the
    response builder applies request values first. LLM or Ollama failures are
    logged and leave the affected fields None.
    """
    if not isinstance(file_path, str) or not file_path.strip():
        raise ValueError("A valid document file path is required")
    supplied = {
        field for field, value in request_values(request_tender).items()
        if value is not None
    }
    missing = [field for field in SUMMARY_FIELDS if field not in supplied]
    result = {field: None for field in SUMMARY_FIELDS}
    if not missing:
        return result

    pages = _load_pages(file_path)
    full_text = "\n\n".join(
        page.get("text") or "" for page in pages if isinstance(page, dict)
    )
    deterministic = {
        "tenderReference": find_tender_reference(full_text),
        "submissionDeadline": find_submission_deadline(full_text),
    }
    for field, value in deterministic.items():
        if field in missing:
            result[field] = value

    needs_llm = [field for field in missing if result[field] is None]
    if not needs_llm:
        print(
            "TENDER_SUMMARY llm_calls=0 reason=deterministic_complete "
            f"fields={sorted(f for f in missing if result[f] is not None)}",
            flush=True,
        )
        return result

    opening_text = _opening_text(pages)
    sidecar_path = _sidecar_path(os.path.abspath(file_path))
    cache_key = _cache_key(opening_text)
    raw = _load_cached_llm(sidecar_path, cache_key) if resume else None
    cache_hit = raw is not None
    if raw is None:
        try:
            raw = ollama_client.extract_tender_summary(opening_text)
        except Exception as exc:
            print(
                "TENDER_SUMMARY status=FAILED "
                f"error_type={type(exc).__name__}; fields left null",
                flush=True,
            )
            return result
        _save_sidecar(sidecar_path, {
            "document": os.path.abspath(file_path),
            "cache_key": cache_key,
            "prompt_version": ollama_client.TENDER_SUMMARY_PROMPT_VERSION,
            "model": ollama_client.MODEL_NAME,
            "llm_raw": raw,
        })

    verified, rejected = verify_llm_values(raw, full_text)
    for field in needs_llm:
        result[field] = verified.get(field)
    print(
        f"TENDER_SUMMARY llm_calls={0 if cache_hit else 1} "
        f"cache_hit={int(cache_hit)} "
        f"populated={sorted(f for f in SUMMARY_FIELDS if result[f] is not None)} "
        f"rejected_unsupported={sorted(rejected)}",
        flush=True,
    )
    return result
