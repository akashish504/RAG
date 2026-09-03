from pipeline.common.ids import chunk_id, document_hash
from pipeline.embedding_pipeline.models import ChunkType


def test_document_hash_is_sha256_hex() -> None:
    assert (
        document_hash(b"sample")
        == "af2bdbe1aa9b6ec1e2adE1d694f41fc71a831d0268e9891562113d8a62add1bf".lower()
    )


def test_parent_chunk_id_is_deterministic() -> None:
    value = chunk_id(
        document_hash_value="a" * 64,
        chunk_type=ChunkType.PARENT,
        parent_position=3,
    )

    assert value == "aaaaaaaaaaaaaaaa:parent:3"


def test_child_chunk_id_includes_parent_and_child_positions() -> None:
    value = chunk_id(
        document_hash_value="b" * 64,
        chunk_type=ChunkType.CHILD,
        parent_position=2,
        child_position=5,
    )

    assert value == "bbbbbbbbbbbbbbbb:child:2:5"
