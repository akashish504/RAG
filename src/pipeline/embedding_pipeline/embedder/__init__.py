"""Embedder stage — produce dense vectors for child chunks."""

from pipeline.embedding_pipeline.embedder.base import Embedder
from pipeline.embedding_pipeline.embedder.stub import StubEmbedder

__all__ = ["Embedder", "StubEmbedder"]
