from __future__ import annotations

import hashlib
import re

MAX_CHARS = 400
MIN_CHARS = 150

_SENTENCE_END = re.compile(r"(?<=[.!?。？！…”\"'」』])\s+")


def split_units(paragraph: str) -> list[str]:
    """Break one paragraph into pieces of at most MAX_CHARS, preferring sentence boundaries."""
    if len(paragraph) <= MAX_CHARS:
        return [paragraph]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > MAX_CHARS:
            cut = max(sentence.rfind(sep, 0, MAX_CHARS) for sep in (", ", "，", " "))
            cut = cut + 1 if cut > MAX_CHARS // 3 else MAX_CHARS
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > MAX_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return [p for p in pieces if p]


def _is_boundary(unit: str) -> bool:
    return int(hashlib.md5(unit.encode("utf-8")).hexdigest(), 16) % 3 == 0


def chunk_paragraphs(paragraphs: list[str]) -> list[str]:
    """Pack paragraphs into TTS-sized chunks.

    Boundaries come from the text itself (size limit, or a content hash of the unit once MIN_CHARS is reached) so an
    edit only changes nearby chunks; later chunks keep identical text and therefore reuse their cached audio.
    """
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for paragraph in paragraphs:
        for unit in split_units(paragraph):
            if current and length + 1 + len(unit) > MAX_CHARS:
                chunks.append("\n".join(current))
                current, length = [], 0
            current.append(unit)
            length += len(unit) + (1 if len(current) > 1 else 0)
            if length >= MIN_CHARS and _is_boundary(unit):
                chunks.append("\n".join(current))
                current, length = [], 0
    if current:
        chunks.append("\n".join(current))
    return chunks


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
