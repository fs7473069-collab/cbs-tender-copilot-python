from dataclasses import dataclass


@dataclass
class Requirement:
    text: str
    category: str
    mandatory: bool
    page: int
    confidence: str


def create_requirement(
    text,
    category,
    mandatory,
    page,
    confidence
):
    return Requirement(
        text=text,
        category=category,
        mandatory=mandatory,
        page=page,
        confidence=confidence
    )


def prepare_llm_input(chunk):
    return {
        "page": chunk["page"],
        "text": chunk["text"]
    }