"""Heading-based chunking of knowledge-base Markdown (SDD "База знань і RAG", step 1)."""

import itertools
from pathlib import Path

import pytest

from rozmova_engine.rag.chunking import (
    PATH_SEPARATOR,
    SECTION_SEPARATOR,
    chunk_markdown,
    count_tokens,
    parse_markdown,
)
from rozmova_engine.rag.ingest import kb_files

KB_DIR = Path(__file__).resolve().parents[3] / "kb"
KB_DOCUMENTS = kb_files(KB_DIR)

HEADER = "# Тариф «Тест»\n\nКод тарифу: `test`"


def _section(heading: str, sentences: int, word: str = "тариф") -> str:
    """A ``##`` section of short Ukrainian sentences about one subject."""
    body = " ".join(f"Речення {i} про {word} «Тест» і його умови." for i in range(sentences))
    return f"## {heading}\n\n{body}"


def _paragraphs(heading: str, count: int) -> str:
    """A ``##`` section of ``count`` numbered three-sentence paragraphs, ~70 tokens each."""
    paragraphs = [
        f"Абзац {i}. Абонентська плата тарифу «Тест» списується щомісяця. "
        f"Залишок хвилин переноситься на наступний місяць, позиція {i}."
        for i in range(count)
    ]
    return f"## {heading}\n\n" + "\n\n".join(paragraphs)


def test_kb_documents_are_found_without_readme() -> None:
    names = {path.name for path in KB_DOCUMENTS}

    assert "README.md" not in names
    assert {"bezlim.md", "roaming.md", "faq.md"} <= names


@pytest.mark.parametrize("path", KB_DOCUMENTS, ids=lambda path: path.stem)
def test_kb_chunks_fit_limits_and_carry_the_document_header(path: Path) -> None:
    markdown = path.read_text(encoding="utf-8")
    parsed = parse_markdown(markdown, path.stem)

    chunks = chunk_markdown(markdown, default_title=path.stem)

    assert chunks
    for chunk in chunks:
        assert chunk.tokens == count_tokens(chunk.text) <= 800
        assert chunk.text.startswith(parsed.header)
    # Every section is covered once and in order: the KB has no oversized sections.
    labels = [label for chunk in chunks for label in chunk.section.split(SECTION_SEPARATOR)]
    assert labels == [path for path, _ in parsed.sections]
    # Merging stops at 300 tokens, so only a document's last chunk may be shorter.
    assert all(chunk.tokens >= 300 for chunk in chunks[:-1])


def test_tariff_chunks_include_the_tariff_code() -> None:
    markdown = (KB_DIR / "tariffs" / "bezlim.md").read_text(encoding="utf-8")

    chunks = chunk_markdown(markdown, default_title="bezlim")

    assert len(chunks) > 1
    assert all("Код тарифу: `bezlim`" in chunk.text for chunk in chunks)


def test_small_sections_are_merged_until_min_tokens() -> None:
    markdown = "\n\n".join(
        [HEADER, _section("Перший", 3), _section("Другий", 3), _section("Третій", 3)]
    )

    chunks = chunk_markdown(markdown, default_title="test", min_tokens=150, max_tokens=800)

    assert [chunk.section for chunk in chunks] == [
        f"Перший{SECTION_SEPARATOR}Другий",
        "Третій",
    ]
    assert "## Перший" in chunks[0].text
    assert "## Другий" in chunks[0].text
    assert chunks[0].tokens >= 150


def test_merging_never_exceeds_max_tokens() -> None:
    markdown = "\n\n".join([HEADER, _section("Перший", 20), _section("Другий", 20)])
    together = count_tokens(markdown)

    chunks = chunk_markdown(
        markdown, default_title="test", min_tokens=together - 1, max_tokens=together - 1
    )

    assert [chunk.section for chunk in chunks] == ["Перший", "Другий"]


def test_subsections_get_a_heading_path() -> None:
    markdown = (
        f"{HEADER}\n\n## Роумінг\n\nВступ.\n\n### Зона 1\n\nЦіни зони 1.\n\n## Інше\n\nТекст."
    )

    parsed = parse_markdown(markdown, "test")

    assert [path for path, _ in parsed.sections] == [
        "Роумінг",
        f"Роумінг{PATH_SEPARATOR}Зона 1",
        "Інше",
    ]


def test_document_without_title_uses_the_default() -> None:
    chunks = chunk_markdown("## Розділ\n\nТекст розділу.", default_title="faq")

    assert chunks[0].text.startswith("# faq\n\n## Розділ")


def test_document_without_sections_is_one_chunk() -> None:
    chunks = chunk_markdown("# Довідка\n\nКороткий текст без розділів.", default_title="x")

    assert len(chunks) == 1
    assert chunks[0].section == "Довідка"


def test_oversized_section_is_split_into_overlapping_windows() -> None:
    max_tokens = 300
    prefix = f"{HEADER}\n\n## Довгий розділ\n\n"
    markdown = f"{HEADER}\n\n{_paragraphs('Довгий розділ', 20)}"

    chunks = chunk_markdown(markdown, default_title="test", min_tokens=100, max_tokens=max_tokens)

    assert len(chunks) > 2
    for chunk in chunks:
        assert chunk.tokens <= max_tokens
        assert chunk.section == "Довгий розділ"
        assert chunk.text.startswith(prefix)
    bodies = [chunk.text.removeprefix(prefix) for chunk in chunks]
    # Each window opens with the tail of the previous one (whole paragraphs, then trailing
    # sentences of a paragraph too long to repeat), at most 15% of max_tokens.
    for previous, current in itertools.pairwise(bodies):
        # The first paragraph whose number the previous window does not contain.
        new_start = next(
            i
            for i in range(len(current))
            if current.startswith("Абзац ", i) and current[i:].split(".")[0] not in previous
        )
        shared = current[:new_start].strip()
        assert shared, "windows must overlap"
        assert previous.endswith(shared)
        assert count_tokens(shared) <= int(max_tokens * 0.15)
    numbers = {f"позиція {i}." for i in range(20)}
    assert all(any(n in body for body in bodies) for n in numbers)


def test_zero_overlap_gives_disjoint_windows() -> None:
    markdown = f"{HEADER}\n\n{_paragraphs('Довгий розділ', 20)}"

    chunks = chunk_markdown(
        markdown, default_title="test", min_tokens=100, max_tokens=300, overlap=0.0
    )

    seen: list[str] = []
    for chunk in chunks:
        seen.extend(p for p in chunk.text.split("\n\n") if p.startswith("Абзац"))
    assert len(seen) == len(set(seen)) == 20


def test_oversized_paragraph_is_split_by_sentences() -> None:
    markdown = f"{HEADER}\n\n{_section('Один абзац', 60)}"

    chunks = chunk_markdown(markdown, default_title="test", min_tokens=100, max_tokens=300)

    assert len(chunks) > 1
    assert all(chunk.tokens <= 300 for chunk in chunks)
    assert "Речення 59 про тариф" in chunks[-1].text


def test_hash_is_stable_and_depends_on_text_and_salt() -> None:
    markdown = "\n\n".join([HEADER, _section("Перший", 30), _section("Другий", 30)])
    edited = markdown.replace("Речення 29 про тариф", "Речення 29 про послугу", 1)

    first = chunk_markdown(markdown, default_title="test", hash_salt="model-a")
    again = chunk_markdown(markdown, default_title="test", hash_salt="model-a")
    other_model = chunk_markdown(markdown, default_title="test", hash_salt="model-b")
    changed = chunk_markdown(edited, default_title="test", hash_salt="model-a")

    assert [c.content_hash for c in first] == [c.content_hash for c in again]
    assert not {c.content_hash for c in first} & {c.content_hash for c in other_model}
    assert first[0].content_hash != changed[0].content_hash
    assert first[1].content_hash == changed[1].content_hash


@pytest.mark.parametrize(
    ("min_tokens", "max_tokens", "overlap"), [(0, 800, 0.15), (900, 800, 0.15), (300, 800, 1.0)]
)
def test_invalid_limits_are_rejected(min_tokens: int, max_tokens: int, overlap: float) -> None:
    with pytest.raises(ValueError, match="min_tokens"):
        chunk_markdown(
            HEADER,
            default_title="test",
            min_tokens=min_tokens,
            max_tokens=max_tokens,
            overlap=overlap,
        )
