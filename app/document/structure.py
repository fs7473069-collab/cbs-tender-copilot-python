"""Section and clause references established from tender document text.

Computed separately from ``create_chunks`` so chunk content, and therefore the
resume fingerprint of existing extraction checkpoints, does not change.
"""

import re

# Upper-case "SECTION" only; prose such as "this section" is not a heading.
_SECTION_RE = re.compile(r"^SECTION\s*[–—-]?\s*([IVXLC]+|\d{1,2})\b\.?\s*(.*)$")
# Short standalone annex/appendix heading lines followed by an upper-case
# title; wrapped prose references ("... as per\nAnnexure-B.") end in
# punctuation. Numbered lists inside annexes (compliance checklists, BOQ rows,
# forms) are not clauses.
_ANNEX_RE = re.compile(
    r"^(?:Annex(?:ure)?|Appendix)\b.{0,40}(?<![.,;:)])$", re.IGNORECASE
)
# Multi-level numbers ("1.2.1") or top-level numbers with a trailing dot
# ("11."). A bare integer line is usually a page number and is ignored.
_CLAUSE_RE = re.compile(
    r"^(?:(\d{1,2}(?:\.\d{1,2}){1,3})\.?|(\d{1,2})\.)(?:\s+(.*))?$"
)
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
MIN_LOCATE_OVERLAP = 0.5


def _lines(text):
    return [line.strip() for line in (text or "").splitlines()]


def _next_nonempty(lines, start):
    for line in lines[start:]:
        if line:
            return line
    return ""


def _starts_like_text(value):
    return bool(value) and (value[0].isalpha() or value[0] in "(\"'“‘")


def _section_title(rest, lines, index):
    candidate = rest.strip() or _next_nonempty(lines, index + 1)
    if (
        candidate
        and any(char.isalpha() for char in candidate)
        and candidate == candidate.upper()
        and not _CLAUSE_RE.match(candidate)
    ):
        return candidate
    return ""


def _clause_major(clause):
    return int(clause.split(".")[0])


class _State:
    def __init__(self):
        self.section = None
        self.clause = None
        self.in_annex = False


def _scan_text_chunk(text, state):
    """Split one text chunk into segments carrying the reference in effect."""
    lines = _lines(text)
    segments = [{"section": state.section, "clause": state.clause, "lines": []}]

    def start_segment():
        segments.append({
            "section": state.section, "clause": state.clause, "lines": [],
        })

    for index, line in enumerate(lines):
        section_match = _SECTION_RE.match(line)
        annex = (
            section_match is None
            and _ANNEX_RE.match(line)
            and _section_title("", lines, index)
        )
        if section_match or annex:
            if section_match:
                heading = " ".join(
                    line[:section_match.start(2)].split()
                ).rstrip(".")
                title = _section_title(section_match.group(2), lines, index)
            else:
                heading = " ".join(line.split())
                title = _section_title("", lines, index)
            state.section = f"{heading}: {title}" if title else heading
            state.clause = None
            state.in_annex = bool(annex)
            start_segment()
            segments[-1]["lines"].append(line)
            continue

        clause_match = None if state.in_annex else _CLAUSE_RE.match(line)
        if clause_match:
            number = clause_match.group(1) or clause_match.group(2)
            rest = clause_match.group(3) or ""
            follows = rest or _next_nonempty(lines, index + 1)
            if _starts_like_text(follows):
                if state.clause is None or _clause_major(number) >= _clause_major(
                    state.clause
                ):
                    state.clause = number
                else:
                    # Restarted numbering without a section heading (contents
                    # lists, checklists): the reference is no longer certain.
                    state.clause = None
                start_segment()
        segments[-1]["lines"].append(line)

    return [
        {
            "section": segment["section"],
            "clause": segment["clause"],
            "text": "\n".join(segment["lines"]),
        }
        for segment in segments
        if any(segment["lines"]) or len(segments) == 1
    ]


def describe_chunk_structure(chunks):
    """Return per-chunk segments ``[{section, clause, text}]`` in chunk order.

    Table chunks inherit a reference only when the page's text has a single
    section/clause throughout; otherwise their reference stays unknown.
    """
    state = _State()
    structures = [None] * len(chunks)
    page_refs = {}

    for position, chunk in enumerate(chunks):
        if chunk.get("source_type", "text") == "text":
            segments = _scan_text_chunk(chunk.get("text", ""), state)
            structures[position] = segments
            refs = page_refs.setdefault(chunk.get("page"), set())
            refs.update((seg["section"], seg["clause"]) for seg in segments)

    for position, chunk in enumerate(chunks):
        if structures[position] is not None:
            continue
        refs = page_refs.get(chunk.get("page")) or set()
        sections = {section for section, _clause in refs}
        section = next(iter(sections)) if len(sections) == 1 else None
        clause = next(iter(refs))[1] if len(refs) == 1 else None
        structures[position] = [{
            "section": section, "clause": clause, "text": chunk.get("text", ""),
        }]
    return structures


def _tokens(text):
    return set(_TOKEN_RE.findall((text or "").casefold()))


def locate_requirement(segments, requirement_text):
    """Return ``{section, clause}`` for a requirement within its chunk.

    Values are None unless one segment is established: the chunk has a single
    segment, or one segment clearly contains most of the requirement's words.
    """
    unknown = {"section": None, "clause": None}
    if not segments:
        return unknown
    if len(segments) == 1:
        return {"section": segments[0]["section"], "clause": segments[0]["clause"]}

    requirement_tokens = _tokens(requirement_text)
    sections = {segment["section"] for segment in segments}
    fallback = {
        "section": next(iter(sections)) if len(sections) == 1 else None,
        "clause": None,
    }
    if not requirement_tokens:
        return fallback

    scores = sorted(
        (
            (len(requirement_tokens & _tokens(segment["text"]))
             / len(requirement_tokens), index)
            for index, segment in enumerate(segments)
        ),
        reverse=True,
    )
    best_score, best_index = scores[0]
    runner_up = scores[1][0] if len(scores) > 1 else 0.0
    if best_score >= MIN_LOCATE_OVERLAP and best_score > runner_up:
        best = segments[best_index]
        return {"section": best["section"], "clause": best["clause"]}
    return fallback
