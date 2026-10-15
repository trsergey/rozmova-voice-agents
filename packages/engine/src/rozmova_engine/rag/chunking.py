"""Heading-based chunking of knowledge-base Markdown (SDD "База знань і RAG", step 1).

A document is ``# Title``, an optional preamble (the tariff or service code) and ``##`` /
``###`` sections. Every chunk starts with the title and preamble, so the LLM always sees
which tariff or service a fragment is about and its id for ``activate_service``.

Sections are atomic where possible: adjacent sections of one document are merged while
the chunk is under ``min_tokens``; a section that does not fit into ``max_tokens`` is cut
by paragraphs (then sentences) into windows that overlap by ``overlap`` of ``max_tokens``.
Tokens are counted with tiktoken ``cl100k_base``, the tokenizer of the embedding model.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

import tiktoken

SECTION_SEPARATOR = " · "
PATH_SEPARATOR = " › "

_HEADING = re.compile(r"^(#{1,3})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_SENTENCE_END = re.compile(r"(?<=[.!?…;:])\s+")


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    # The BPE file is downloaded once into TIKTOKEN_CACHE_DIR (the image pre-fetches it).
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding().encode(text))


@dataclass(frozen=True, slots=True)
class Chunk:
    """``section`` names the heading(s) the chunk covers; ``text`` is what gets embedded."""

    section: str
    text: str
    tokens: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    title: str
    header: str
    sections: list[tuple[str, str]]  # (heading path, Markdown including the heading line)


def parse_markdown(markdown: str, default_title: str) -> ParsedDocument:
    """Split a document into its header (title and preamble) and ``##``/``###`` sections."""
    title = default_title
    header: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    parent: str | None = None

    for line in markdown.splitlines():
        match = _HEADING.match(line)
        level = len(match.group(1)) if match else 0
        if match and level == 1 and not sections and title == default_title:
            title = match.group(2)
            header.append(line)
        elif match and level >= 2:
            name = match.group(2)
            if level == 2:
                parent = name
                path = name
            else:
                path = f"{parent}{PATH_SEPARATOR}{name}" if parent else name
            sections.append((path, [line]))
        elif sections:
            sections[-1][1].append(line)
        else:
            header.append(line)

    if not any(line.strip().startswith("#") for line in header):
        header.insert(0, f"# {title}")
    return ParsedDocument(
        title=title,
        header=_clean("\n".join(header)),
        sections=[(path, _clean("\n".join(lines))) for path, lines in sections],
    )


def chunk_markdown(
    markdown: str,
    *,
    default_title: str,
    min_tokens: int = 300,
    max_tokens: int = 800,
    overlap: float = 0.15,
    hash_salt: str = "",
) -> list[Chunk]:
    """Chunk one Markdown document; ``hash_salt`` (the embedding model) goes into each hash,
    so switching models re-embeds every chunk."""
    if not 0 < min_tokens <= max_tokens or not 0 <= overlap < 1:
        raise ValueError("need 0 < min_tokens <= max_tokens and 0 <= overlap < 1")
    doc = parse_markdown(markdown, default_title)
    if count_tokens(doc.header) >= max_tokens // 2:
        raise ValueError(f"the preamble of {doc.title!r} leaves no room for sections")

    def render(parts: list[str]) -> str:
        return "\n\n".join([doc.header, *parts])

    pieces: list[tuple[str, str]] = []  # (section label, text)
    group: list[tuple[str, str]] = []

    def flush() -> None:
        if group:
            label = SECTION_SEPARATOR.join(path for path, _ in group)
            pieces.append((label, render([body for _, body in group])))
            group.clear()

    if not doc.sections:
        pieces.append((doc.title, doc.header))
    for path, body in doc.sections:
        if count_tokens(render([body])) > max_tokens:
            flush()
            pieces.extend((path, text) for text in _windows(body, render, max_tokens, overlap))
            continue
        candidate = render([b for _, b in group] + [body])
        if group and (
            count_tokens(render([b for _, b in group])) >= min_tokens
            or count_tokens(candidate) > max_tokens
        ):
            flush()
        group.append((path, body))
    flush()

    return [
        Chunk(
            section=label,
            text=text,
            tokens=count_tokens(text),
            content_hash=_hash(hash_salt, label, text),
        )
        for label, text in pieces
    ]


def _windows(
    section: str, render: Callable[[list[str]], str], max_tokens: int, overlap: float
) -> list[str]:
    """Cut an oversized section into windows of whole units with an overlapping tail.

    Every window repeats the section heading. Units are paragraphs, or sentences of a
    paragraph that is too long, or token slices of a sentence that is still too long.
    """
    heading, _, body = section.partition("\n")
    budget = max_tokens - count_tokens(render([heading])) - 2
    units = [unit for paragraph in _paragraphs(body) for unit in _split_unit(paragraph, budget)]
    overlap_budget = int(max_tokens * overlap)

    def text_of(window: list[str]) -> str:
        return render([heading + "\n\n" + "\n\n".join(window)])

    windows: list[str] = []
    carry: list[str] = []
    i = 0
    while i < len(units):
        window = list(carry)
        added = 0
        while i < len(units) and count_tokens(text_of([*window, units[i]])) <= max_tokens:
            window.append(units[i])
            i += 1
            added += 1
        if added == 0:
            if not carry:  # cannot happen: every unit fits the budget on its own
                raise AssertionError("a unit is larger than the window")
            carry = []
            continue
        windows.append(text_of(window))
        carry = _tail(window, overlap_budget)
    return windows


def _tail(window: list[str], budget: int) -> list[str]:
    """The overlap carried into the next window: the longest run of trailing units within
    ``budget`` tokens, finished with trailing sentences of the unit that does not fit.
    It never repeats a whole window, so every window adds new text."""
    tail: list[str] = []
    used = 0
    for index in range(len(window) - 1, -1, -1):
        unit = window[index]
        size = count_tokens(unit)
        if index > 0 and used + size <= budget:
            tail.insert(0, unit)
            used += size
            continue
        sentences: list[str] = []
        for sentence in reversed(_SENTENCE_END.split(unit)[1:]):
            used += count_tokens(sentence) + 1
            if used > budget:
                break
            sentences.insert(0, sentence)
        if sentences:
            tail.insert(0, " ".join(sentences))
        break
    return tail


def _paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def _split_unit(paragraph: str, budget: int) -> list[str]:
    if count_tokens(paragraph) <= budget:
        return [paragraph]
    units: list[str] = []
    for sentence in _SENTENCE_END.split(paragraph):
        if count_tokens(sentence) <= budget:
            units.append(sentence)
            continue
        tokens = _encoding().encode(sentence)
        units.extend(
            _encoding().decode(tokens[start : start + budget])
            for start in range(0, len(tokens), budget)
        )
    return units


def _clean(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
