from pathlib import Path

import pytest

from pipeline.embedding_pipeline.chunker.base import ChunkingConfig
from pipeline.embedding_pipeline.chunker.registry import default_chunker_registry
from pipeline.embedding_pipeline.chunker.resume import (
    ResumeChunker,
    canonical_section,
    split_section_into_entries,
)
from pipeline.embedding_pipeline.models import ChunkType, Document, SourceMetadata
from pipeline.embedding_pipeline.parser.text import TextParser

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "samples"


def make_document(text: str) -> Document:
    body = text.encode("utf-8")
    source = SourceMetadata(
        s3_bucket="test-bucket",
        s3_key="raw/profile/recABC/cv.txt",
        table_name="profile",
        primary_key="recABC",
        column_name="cv",
        filename="cv.txt",
        source_url="s3://test-bucket/raw/profile/recABC/cv.txt",
    )
    return Document(
        text=text,
        body=body,
        document_hash="cd" * 32,
        source=source,
        content_length=len(body),
    )


def chunk_resume(text: str, *, config: ChunkingConfig | None = None):
    parsed = TextParser().parse(text.encode("utf-8"))
    document = make_document(text)
    chunker = ResumeChunker()
    return chunker.chunk(
        document=document,
        parsed=parsed,
        config=config or ChunkingConfig(parent_max_tokens=1200, child_max_tokens=250),
    )


# ---------------------------------------------------------------------------
# Canonicalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("Profile", "summary"),
        ("PROFESSIONAL SUMMARY", "summary"),
        ("Skills", "skills"),
        ("Core Competencies", "skills"),
        ("Experience", "experience"),
        ("Work Experience", "experience"),
        ("EMPLOYMENT HISTORY", "experience"),
        ("Education", "education"),
        ("Selected Projects", "projects"),
        ("Certifications", "certifications"),
        ("Random Heading", "other"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_canonical_section(raw, canonical):
    assert canonical_section(raw) == canonical


# ---------------------------------------------------------------------------
# Entry splitting
# ---------------------------------------------------------------------------


def test_split_section_into_entries_uses_pipe_header_as_marker():
    body = (
        "Senior Engineer | Acme | Jan 2020 - Present\n"
        "- Led search platform.\n"
        "\n"
        "Software Engineer | Beta | Jun 2017 - Dec 2019\n"
        "- Built pipelines.\n"
    )
    entries = split_section_into_entries(body)
    assert len(entries) == 2
    assert entries[0].startswith("Senior Engineer | Acme")
    assert entries[1].startswith("Software Engineer | Beta")


def test_split_section_into_entries_falls_back_to_paragraphs():
    body = "First paragraph here.\n\nSecond paragraph here."
    entries = split_section_into_entries(body)
    assert entries == ["First paragraph here.", "Second paragraph here."]


def test_split_section_into_entries_groups_continuation_paragraphs():
    body = (
        "Senior Engineer | Acme | Jan 2020 - Present\n"
        "- Led platform.\n"
        "\n"
        "Outcome: 40% latency reduction.\n"
        "\n"
        "Software Engineer | Beta | Jun 2017 - Dec 2019\n"
        "- Built pipelines.\n"
    )
    entries = split_section_into_entries(body)
    assert len(entries) == 2
    assert "Outcome: 40% latency reduction." in entries[0]


# ---------------------------------------------------------------------------
# End-to-end resume chunking
# ---------------------------------------------------------------------------


def _load_sample_resume() -> str:
    return (FIXTURES / "sample_resume.txt").read_text()


def test_resume_chunker_emits_one_parent_per_canonical_section():
    chunks = chunk_resume(_load_sample_resume())
    parents = [c for c in chunks if c.chunk_type is ChunkType.PARENT]

    canonicals = [p.metadata["section_canonical"] for p in parents]
    assert canonicals == [
        "summary",
        "skills",
        "experience",
        "education",
        "projects",
        "certifications",
    ]


def test_experience_section_emits_one_child_per_entry():
    chunks = chunk_resume(_load_sample_resume())
    parents = [c for c in chunks if c.chunk_type is ChunkType.PARENT]
    experience_parent = next(
        p for p in parents if p.metadata["section_canonical"] == "experience"
    )

    children = [
        c
        for c in chunks
        if c.chunk_type is ChunkType.CHILD
        and c.parent_chunk_id == experience_parent.chunk_id
    ]
    assert len(children) == 3

    headers = [child.text.split("\n", 1)[0] for child in children]
    assert any("Senior Engineer | Acme Corp" in h for h in headers)
    assert any("Software Engineer | Beta Inc" in h for h in headers)
    assert any("Software Engineer Intern | Gamma Labs" in h for h in headers)


def test_children_are_linked_to_parent_for_traceability():
    chunks = chunk_resume(_load_sample_resume())
    parent_ids = {c.chunk_id for c in chunks if c.chunk_type is ChunkType.PARENT}

    for child in (c for c in chunks if c.chunk_type is ChunkType.CHILD):
        assert child.parent_chunk_id in parent_ids
        assert child.metadata["table_name"] == "profile"
        assert child.metadata["document_id"] == "recABC"
        assert child.metadata["s3_path"] == "raw/profile/recABC/cv.txt"
        assert child.metadata["strategy"] == "resume"


def test_short_non_list_section_emits_exactly_one_child():
    # Non-list sections (e.g. Skills) must always produce at least one child
    # so their text is embedded and findable via KNN search.
    # A short section that fits in a single token window → exactly one child.
    chunks = chunk_resume(_load_sample_resume())
    parents = [c for c in chunks if c.chunk_type is ChunkType.PARENT]
    skills_parent = next(
        p for p in parents if p.metadata["section_canonical"] == "skills"
    )

    skills_children = [
        c
        for c in chunks
        if c.chunk_type is ChunkType.CHILD
        and c.parent_chunk_id == skills_parent.chunk_id
    ]
    assert len(skills_children) == 1
    assert skills_children[0].text == skills_parent.text


def test_entries_are_not_split_when_each_fits_under_child_max_tokens():
    chunks = chunk_resume(_load_sample_resume())
    experience_children = [
        c
        for c in chunks
        if c.chunk_type is ChunkType.CHILD
        and c.metadata.get("section_canonical") == "experience"
    ]
    # Each entry should appear in exactly one child (no entry_sub_index).
    sub_indexes = [c.metadata.get("entry_sub_index") for c in experience_children]
    assert all(idx is None for idx in sub_indexes)
    entry_indexes = [c.metadata["entry_index"] for c in experience_children]
    assert sorted(entry_indexes) == [0, 1, 2]


def test_oversized_entry_is_window_split_but_keeps_same_entry_index():
    # Single very long Experience entry — should fall back to token windows
    # tagged with the same entry_index so retrieval can group them.
    long_bullets = "\n".join(["- " + ("really detailed achievement " * 20)] * 30)
    text = (
        "# Experience\n\n"
        "Senior Engineer | Acme | Jan 2020 - Present\n"
        f"{long_bullets}\n"
    )
    chunks = chunk_resume(
        text,
        config=ChunkingConfig(
            parent_max_tokens=2000,
            parent_overlap_tokens=100,
            child_max_tokens=120,
            child_overlap_tokens=20,
            child_min_tokens=20,
        ),
    )
    children = [c for c in chunks if c.chunk_type is ChunkType.CHILD]
    assert len(children) > 1
    assert all(c.metadata["entry_index"] == 0 for c in children)
    sub_indexes = sorted(c.metadata.get("entry_sub_index") for c in children)
    assert sub_indexes == list(range(len(children)))


def test_chunk_ids_are_deterministic_across_runs():
    text = _load_sample_resume()
    first = chunk_resume(text)
    second = chunk_resume(text)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_default_registry_exposes_resume_and_parent_child():
    registry = default_chunker_registry()
    names = sorted(registry.names())
    assert "parent_child" in names
    assert "resume" in names
    assert "pptx_slide" in names
    assert isinstance(registry.get("resume"), ResumeChunker)


def test_registry_raises_for_unknown_strategy():
    registry = default_chunker_registry()
    with pytest.raises(KeyError):
        registry.get("does_not_exist")
