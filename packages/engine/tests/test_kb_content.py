"""Consistency checks for the knowledge base content in `kb/` against its catalog."""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

KB_DIR = Path(__file__).resolve().parents[3] / "kb"
# kind → (folder, title prefix, id label)
KINDS = {
    "tariff": ("tariffs", "Тариф", "Код тарифу"),
    "service": ("services", "Послуга", "Код послуги"),
}

# Real Ukrainian operators and the S2 "not found" tariff must not appear in ingested documents.
FORBIDDEN = ("київстар", "kyivstar", "vodafone", "водафон", "lifecell", "лайфсел", "космос")

# ~350 Ukrainian words is the 800-token chunk ceiling for cl100k_base (SDD "База знань і RAG").
MAX_SECTION_WORDS = 350


@dataclass(frozen=True)
class CatalogEntry:
    kind: str
    id: str
    name: str
    price: str


def _catalog() -> list[CatalogEntry]:
    readme = (KB_DIR / "README.md").read_text(encoding="utf-8")
    row = re.compile(r"^\| (tariff|service) \| `(\w+)` \| ([^|]+?) \| ([^|]+?) \|$", re.MULTILINE)
    return [CatalogEntry(*match.groups()) for match in row.finditer(readme)]


def _documents() -> list[Path]:
    return sorted(path for path in KB_DIR.rglob("*.md") if path.name != "README.md")


CATALOG = _catalog()


def test_catalog_covers_ticket_scope() -> None:
    tariffs = [entry for entry in CATALOG if entry.kind == "tariff"]
    services = [entry for entry in CATALOG if entry.kind == "service"]

    assert 4 <= len(tariffs) <= 6
    assert 6 <= len(services) <= 10
    assert len({entry.id for entry in CATALOG}) == len(CATALOG)
    assert {"bezlim", "roaming_eu"} <= {entry.id for entry in CATALOG}


@pytest.mark.parametrize("entry", CATALOG, ids=lambda entry: entry.id)
def test_document_matches_catalog(entry: CatalogEntry) -> None:
    folder, title, id_label = KINDS[entry.kind]
    text = (KB_DIR / folder / f"{entry.id}.md").read_text(encoding="utf-8")

    assert text.startswith(f"# {title} «{entry.name}»\n")
    assert f"{id_label}: `{entry.id}`" in text
    amount = entry.price.split(" ")[0]
    assert f"{amount} грн" in text or amount == "0"


def test_every_document_is_in_catalog() -> None:
    ids = {entry.id for entry in CATALOG}
    for folder, _, _ in KINDS.values():
        assert {path.stem for path in (KB_DIR / folder).glob("*.md")} <= ids


def test_ids_in_text_exist_in_catalog() -> None:
    ids = {entry.id for entry in CATALOG}
    for path in _documents():
        text = path.read_text(encoding="utf-8")
        mentioned = set(re.findall(r"Код (?:тарифу|послуги):? `(\w+)`", text))
        mentioned |= set(re.findall(r"код послуги `(\w+)`", text))
        assert mentioned <= ids, path.name


@pytest.mark.parametrize("path", _documents(), ids=lambda path: path.name)
def test_document_is_clean_and_chunkable(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()

    assert text.startswith("# ")
    assert [word for word in FORBIDDEN if word in lowered] == []
    for section in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        heading = section.splitlines()[0]
        assert len(section.split()) <= MAX_SECTION_WORDS, heading
