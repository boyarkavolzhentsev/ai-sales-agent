"""Deterministic chunking. Same input, same chunks, same IDs and hashes.

Markdown bodies are split into sections by ATX headings (``#`` .. ``######``, ignoring
headings inside fenced code), then paragraphs are packed into chunks of at most
``max_chars``. Each chunk starts with its context line: the heading path
("Pricing > Basic plan"), or the document title for text before the first heading.
Over-long paragraphs are split at sentence ends, then at whitespace. No overlap: chunks
are matched lexically, so repeated text would only double-count terms.

Each structured fact becomes its own chunk (after the body chunks), so a fact can be
retrieved, cited and checked for conflicts individually.

chunk_id = "kc_" + first 40 hex chars of SHA-256(source_id, version, ordinal, content hash).
"""

import hashlib
import re

from app.core.models import KnowledgeChunk
from app.knowledge.loader import sha256_text
from app.knowledge.models import Fact, LoadedSource
from app.persistence.records import KnowledgeFactRecord

MAX_CHUNK_CHARS = 1200
_MIN_BUDGET = 200
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def chunk_id_for(source_id: str, version: int, ordinal: int, content_hash: str) -> str:
    digest = hashlib.sha256(f"{source_id}\n{version}\n{ordinal}\n{content_hash}".encode()).hexdigest()
    return f"kc_{digest[:40]}"


def markdown_sections(body: str) -> list[tuple[tuple[str, ...], list[str]]]:
    """(heading path, paragraphs) per section, in document order."""
    sections: list[tuple[tuple[str, ...], list[str]]] = []
    path: list[tuple[int, str]] = []
    paragraphs: list[str] = []
    current: list[str] = []
    in_fence = False

    def close_paragraph() -> None:
        if current:
            paragraphs.append("\n".join(current).strip())
            current.clear()

    def close_section() -> None:
        close_paragraph()
        kept = [p for p in paragraphs if p]
        if kept:
            sections.append((tuple(title for _, title in path), kept))
        paragraphs.clear()

    for line in body.split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            current.append(line)
            continue
        heading = None if in_fence else _HEADING.match(line)
        if heading:
            close_section()
            level = len(heading.group(1))
            path[:] = [(lvl, title) for lvl, title in path if lvl < level]
            path.append((level, heading.group(2).strip()))
        elif not in_fence and not line.strip():
            close_paragraph()
        else:
            current.append(line.rstrip())
    close_section()
    return sections


def _split_long(paragraph: str, budget: int) -> list[str]:
    if len(paragraph) <= budget:
        return [paragraph]
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > budget:
            cut = sentence.rfind(" ", 0, budget)
            cut = cut if cut > 0 else budget
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    return pieces


def _pack(paragraphs: list[str], budget: int) -> list[str]:
    packed: list[str] = []
    buffer = ""
    for piece in (p for paragraph in paragraphs for p in _split_long(paragraph, budget)):
        candidate = f"{buffer}\n\n{piece}" if buffer else piece
        if len(candidate) <= budget:
            buffer = candidate
        else:
            if buffer:
                packed.append(buffer)
            buffer = piece
    if buffer:
        packed.append(buffer)
    return packed


def _fact_text(title: str, fact: Fact) -> str:
    unit = f" {fact.unit}" if fact.unit else ""
    lines = [fact.statement] if fact.statement else []
    lines.append(f"{fact.key} = {fact.value}{unit}")
    return f"{title}\n\n" + "\n".join(lines)


def chunk_source(
    loaded: LoadedSource, *, max_chars: int = MAX_CHUNK_CHARS
) -> tuple[tuple[KnowledgeChunk, ...], tuple[KnowledgeFactRecord, ...]]:
    source = loaded.source
    texts: list[str] = []
    for heading_path, paragraphs in markdown_sections(loaded.body):
        context = " > ".join(heading_path) if heading_path else source.title
        budget = max(max_chars - len(context) - 2, _MIN_BUDGET)
        texts.extend(f"{context}\n\n{packed}" for packed in _pack(paragraphs, budget))
    fact_start = len(texts)
    texts.extend(_fact_text(source.title, fact) for fact in loaded.facts)

    chunks: list[KnowledgeChunk] = []
    for ordinal, text in enumerate(texts):
        content_hash = sha256_text(text)
        chunks.append(
            KnowledgeChunk(
                chunk_id=chunk_id_for(source.source_id, source.version, ordinal, content_hash),
                source_id=source.source_id,
                source_version=source.version,
                ordinal=ordinal,
                text=text,
                content_hash=content_hash,
            )
        )
    facts = tuple(
        KnowledgeFactRecord(
            source_id=source.source_id,
            source_version=source.version,
            fact_key=fact.key,
            value=fact.value,
            unit=fact.unit,
            chunk_id=chunks[fact_start + index].chunk_id,
        )
        for index, fact in enumerate(loaded.facts)
    )
    return tuple(chunks), facts
