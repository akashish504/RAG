"""Embedding pipeline flushes embed+index in bounded groups (not all-at-end), so a
crash can't lose everything and RAM stays bounded. The loader/embedder/indexer are faked.
"""

from __future__ import annotations

from types import SimpleNamespace

import pipeline.embedding_pipeline.pipeline as pipe_mod
from pipeline.embedding_pipeline.models import (
    Chunk,
    ChunkType,
    EmbedReport,
    IndexReport,
    SourceMetadata,
)
from pipeline.embedding_pipeline.pipeline import Pipeline


def _child(i: int) -> Chunk:
    return Chunk(
        chunk_id=f"c{i}", chunk_type=ChunkType.CHILD, text="x", token_count=1, position=0,
        document_hash=f"h{i}",
        source=SourceMetadata(s3_bucket="b", s3_key=f"raw/d.quals/{i}/f__normalized.txt",
                              table_name="d_quals", primary_key=str(i), column_name="c", filename="f"),
    )


def _loaded(i: int):
    doc = SimpleNamespace(
        document_hash=f"h{i}",
        source=SimpleNamespace(s3_key=f"raw/d.quals/{i}/f__normalized.txt", s3_bucket="b",
                               source_url=None, table_name="d_quals", primary_key=str(i),
                               column_name="c"),
    )
    table = SimpleNamespace(chunker_strategy="pptx_slide", chunking=None)
    return SimpleNamespace(document=doc, parsed=None, table=table)


class _Embedder:
    def __init__(self): self.calls = 0
    def embed(self, children):
        self.calls += 1
        for c in children:
            c.embedding = [0.0]
        return EmbedReport(chunks_embedded=len(children), batches_sent=1)


class _Indexer:
    def __init__(self): self.indexed_docs = 0
    def document_hash_exists(self, h): return False
    def delete_by_s3_key(self, k): return 0
    def index(self, chunks):
        self.indexed_docs += 1
        return IndexReport(indexed=len(chunks))


class _Chunker:
    # each doc → 3 children (so 4 docs = 12 children; flush at 5 → flushes mid-loop)
    def chunk(self, *, document, parsed, config):  # noqa: ARG002
        i = document.source.primary_key
        return [_child(f"{i}_{j}") for j in range(3)]


def test_pipeline_flushes_incrementally(monkeypatch) -> None:
    monkeypatch.setattr(pipe_mod, "_FLUSH_CHILDREN", 5)  # small threshold → multiple flushes

    p = Pipeline.__new__(Pipeline)
    p._loader = SimpleNamespace(iter_prefix=lambda prefix: iter([_loaded(i) for i in range(4)]))
    p._skip_unchanged = False
    p._indexer = _Indexer()
    p._embedder = _Embedder()
    p._chunker_registry = SimpleNamespace(get=lambda name: _Chunker())
    p._pg_doc_logger = None

    report = p.run(prefix="raw/d.quals/")

    # 4 docs × 3 children = 12; flushing at 5 → embed called more than once (NOT all-at-end).
    assert p._embedder.calls >= 2
    assert p._indexer.indexed_docs == 4           # every doc indexed
    assert report.index_report.indexed == 12      # every chunk indexed
    assert report.documents_read == 4


class _BigChunker:
    """One doc explodes into many children (simulates corrupt extraction)."""
    def chunk(self, *, document, parsed, config):  # noqa: ARG002
        i = document.source.primary_key
        return [_child(f"{i}_{j}") for j in range(20)]


def test_corrupt_document_is_skipped_not_oom(monkeypatch) -> None:
    monkeypatch.setattr(pipe_mod, "_MAX_CHILDREN_PER_DOC", 5)   # cap below the 20-child doc
    monkeypatch.setattr(pipe_mod, "_FLUSH_CHILDREN", 100)

    p = Pipeline.__new__(Pipeline)
    p._loader = SimpleNamespace(iter_prefix=lambda prefix: iter([_loaded(1)]))
    p._skip_unchanged = False
    p._indexer = _Indexer()
    p._embedder = _Embedder()
    p._chunker_registry = SimpleNamespace(get=lambda name: _BigChunker())
    p._pg_doc_logger = None

    report = p.run(prefix="raw/d.quals/")

    assert p._embedder.calls == 0          # never embedded the giant doc → no OOM
    assert p._indexer.indexed_docs == 0    # not indexed
    assert report.documents_failed == 1    # flagged as failed/skipped
    assert report.documents_read == 0
