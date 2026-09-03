from pipeline.embedding_pipeline.chunker.base import ChunkingConfig
from pipeline.embedding_pipeline.chunker.parent_child import ParentChildChunker
from pipeline.embedding_pipeline.models import ChunkType, Document, SourceMetadata
from pipeline.embedding_pipeline.parser.text import TextParser


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
        document_hash="abcd" * 16,
        source=source,
        content_length=len(body),
    )


def test_chunker_emits_one_parent_per_short_section() -> None:
    text = "# Profile\n\nShort bio.\n\n# Skills\n\nPython, SQL, ML."
    parsed = TextParser().parse(text.encode("utf-8"))
    document = make_document(text)
    chunker = ParentChildChunker()

    chunks = chunker.chunk(
        document=document,
        parsed=parsed,
        config=ChunkingConfig(
            parent_max_tokens=200,
            child_max_tokens=80,
            child_overlap_tokens=20,
        ),
    )

    parents = [c for c in chunks if c.chunk_type is ChunkType.PARENT]
    assert len(parents) == 2
    sections = [p.metadata["section_title"] for p in parents]
    assert sections == ["Profile", "Skills"]


def test_chunker_propagates_provenance_metadata() -> None:
    text = "# Bio\n\nSome content"
    parsed = TextParser().parse(text.encode("utf-8"))
    document = make_document(text)
    chunker = ParentChildChunker()

    chunks = chunker.chunk(
        document=document,
        parsed=parsed,
        config=ChunkingConfig(parent_max_tokens=200, child_max_tokens=80),
    )

    parent = chunks[0]
    assert parent.table_name == "profile"
    assert parent.document_id == "recABC"
    assert parent.s3_path == "raw/profile/recABC/cv.txt"
    assert parent.metadata["table_name"] == "profile"
    assert parent.metadata["document_id"] == "recABC"
    assert parent.metadata["section_title"] == "Bio"


def test_chunker_splits_long_section_into_multiple_parents() -> None:
    long_text = "# Long\n\n" + ("Sentence about consulting work. " * 400)
    parsed = TextParser().parse(long_text.encode("utf-8"))
    document = make_document(long_text)
    chunker = ParentChildChunker()

    chunks = chunker.chunk(
        document=document,
        parsed=parsed,
        config=ChunkingConfig(
            parent_max_tokens=300,
            parent_overlap_tokens=50,
            child_max_tokens=100,
            child_overlap_tokens=20,
        ),
    )

    parents = [c for c in chunks if c.chunk_type is ChunkType.PARENT]
    assert len(parents) > 1
    children = [c for c in chunks if c.chunk_type is ChunkType.CHILD]
    assert all(child.parent_chunk_id is not None for child in children)


def test_chunk_ids_are_deterministic_across_runs() -> None:
    text = "# Bio\n\nSome content"
    parsed = TextParser().parse(text.encode("utf-8"))
    document = make_document(text)
    chunker = ParentChildChunker()

    first = chunker.chunk(document=document, parsed=parsed, config=ChunkingConfig())
    second = chunker.chunk(document=document, parsed=parsed, config=ChunkingConfig())

    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
