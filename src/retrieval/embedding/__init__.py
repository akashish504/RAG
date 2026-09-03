"""Query-side embedders.

Distinct from the indexer's passage-side embedder
(:mod:`pipeline.embedding_pipeline.embedder.voyage`) because Voyage requires
a different prefix for query vs. passage and the runtime concerns (single
text, async, request cache) are different.
"""
