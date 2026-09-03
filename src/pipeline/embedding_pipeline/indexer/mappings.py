"""OpenSearch index mapping and settings for ``mcp-docs``.

Search strategy: hybrid BM25 + KNN, combined via RRF at query time.

- BM25 (``text`` field, ``dalberg_english`` analyser): keyword/lexical matching
  with domain synonym expansion and stemming.
- KNN (``embedding`` field, HNSW cosine): semantic/vector matching.
  Only child chunks carry embeddings; parent chunks are indexed without
  the field.  The KNN pre-filter ``chunk_type = "child"`` ensures vector
  search never touches parent-only documents.

Hybrid queries use Reciprocal Rank Fusion to merge the two ranked lists
without needing to tune score weights.

``section_canonical`` is hoisted to a top-level keyword field (duplicated from
``metadata.section_canonical``) so filter/boost queries work reliably without
relying on dynamic mapping inference.
"""

from __future__ import annotations

import copy
from typing import Any

EMBEDDING_DIMS: int = 1024

# Domain synonyms: each comma-separated group is treated as equivalent at both
# index time and query time.  Add/extend rows as new acronyms are encountered.
_DALBERG_SYNONYMS: list[str] = [
    "esg, environmental social governance, environmental social and governance",
    "ml, machine learning",
    "nlp, natural language processing",
    "ai, artificial intelligence",
    "m&e, monitoring and evaluation, monitoring evaluation",
    "wash, water sanitation hygiene, water sanitation and hygiene",
    "fcas, fragile conflict affected states, fragile and conflict affected states",
    "dfi, development finance institution, development finance institutions",
    "ppp, public private partnership, public private partnerships",
    "ict, information communication technology, information and communication technology",
    "ngo, non-governmental organization, non governmental organization",
    "oda, official development assistance",
    "sdg, sustainable development goals, sustainable development goal",
    "vfm, value for money",
    "rct, randomized controlled trial, randomized controlled trials",
    "lmic, low and middle income country, low and middle income countries",
    "ssa, sub-saharan africa, sub saharan africa",
    "mena, middle east north africa, middle east and north africa",
    "dei, diversity equity inclusion, diversity equity and inclusion",
    "gbv, gender based violence, gender-based violence",
    "srh, sexual reproductive health, sexual and reproductive health",
    "sme, small medium enterprise, small and medium enterprise",
]

INDEX_SETTINGS: dict[str, Any] = {
    "index": {
        "knn": True,
        "knn.algo_param.ef_search": 512,
        # 3-4 primaries for the full library (~1M+ child vectors); 1 shard won't
        # scale. Set at create time (can't change a live index's primary count).
        "number_of_shards": 4,
        "number_of_replicas": 1,
        "refresh_interval": "30s",
    },
    "analysis": {
        "filter": {
            "dalberg_synonyms": {
                "type": "synonym",
                "synonyms": _DALBERG_SYNONYMS,
            },
            "english_stemmer": {"type": "stemmer", "language": "english"},
            "english_stop": {"type": "stop", "stopwords": "_english_"},
        },
        "analyzer": {
            # Drop-in replacement for the built-in "english" analyzer but with
            # synonym expansion applied before stemming.  Synonyms expand at
            # both index time and query time so "ESG" matches
            # "environmental social governance" in either direction.
            "dalberg_english": {
                "tokenizer": "standard",
                "filter": [
                    "lowercase",
                    "dalberg_synonyms",
                    "english_stop",
                    "english_stemmer",
                ],
            }
        },
    },
}

INDEX_MAPPING: dict[str, Any] = {
    "properties": {
        # ---- Vector field (child chunks only) — HNSW cosine, no encoder
        # so it works on AWS OpenSearch < 2.9 which lacks int8 sq support.
        "embedding": {
            "type": "knn_vector",
            "dimension": EMBEDDING_DIMS,
            "method": {
                "name": "hnsw",
                "space_type": "cosinesimil",
                "engine": "lucene",
                "parameters": {"m": 16, "ef_construction": 200},
            },
        },
        # ---- Full-text content ----------------------------------------
        "text": {
            "type": "text",
            "analyzer": "dalberg_english",
            "fields": {
                "keyword": {"type": "keyword", "ignore_above": 2048}
            },
        },
        # ---- Chunk identity ------------------------------------------
        "chunk_id": {"type": "keyword"},
        "chunk_type": {"type": "keyword"},
        "parent_chunk_id": {"type": "keyword"},
        "document_hash": {"type": "keyword"},
        # ---- Numeric / ordering --------------------------------------
        "position": {"type": "integer"},
        "token_count": {"type": "integer"},
        # ---- Provenance ----------------------------------------------
        "table_name": {"type": "keyword"},
        "primary_key": {"type": "keyword"},
        "column_name": {"type": "keyword"},
        "s3_key": {"type": "keyword"},
        "s3_bucket": {"type": "keyword"},
        # Full s3:// URL — makes it easy to locate or delete the source file.
        "source_url": {"type": "keyword"},
        "filename": {"type": "keyword"},
        "airtable_record_id": {"type": "keyword"},
        "airtable_base_id": {"type": "keyword"},
        "airtable_table_id": {"type": "keyword"},
        # "record_summary" marks the record-level parent summary chunks so
        # retrieval can hydrate them alongside child hits from the same record.
        "doc_role": {"type": "keyword"},
        # ---- D.Quals structured facets (filtered-KNN) — keyword/date ----------
        # Record-level; present on every chunk. Multi-selects index as keyword
        # arrays. Other tables simply don't populate these.
        "client_organisation": {"type": "keyword"},
        "practice_area": {"type": "keyword"},
        "project_region": {"type": "keyword"},
        "project_location": {"type": "keyword"},
        "dalberg_entity": {"type": "keyword"},
        "insight_type": {"type": "keyword"},
        "confidential_project": {"type": "keyword"},
        "confidential_client": {"type": "keyword"},
        "start_date": {"type": "date", "ignore_malformed": True},
        "end_date": {"type": "date", "ignore_malformed": True},
        # Backfilled record metadata (scripts/backfill_d_quals_record_meta.py):
        # the project's Dalberg contact (PD) as a filterable keyword, and the
        # long-text project description (display only, not separately indexed).
        # The script also put_mapping's these onto the LIVE index so a pre-existing
        # index gets the correct types before values are patched in.
        "dalberg_contact_person": {"type": "keyword"},
        "project_description": {"type": "text", "index": False},
        # ---- Knowledge Library facets (shared mapping; additive) -------------
        "kd_type": {"type": "keyword"},
        "country_region": {"type": "keyword"},
        "author": {"type": "keyword"},
        "team": {"type": "keyword"},
        "item_type": {"type": "keyword"},
        "client": {"type": "keyword"},
        "language": {"type": "keyword"},
        "can_be_shared_externally": {"type": "keyword"},
        "date_of_publication": {"type": "date", "ignore_malformed": True},
        # ---- Proposal Library facets (shared mapping; additive) --------------
        "country": {"type": "keyword"},
        "region": {"type": "keyword"},
        "project_type": {"type": "keyword"},
        "date": {"type": "date", "ignore_malformed": True},
        # ---- Section / slide (hoisted from metadata for reliable filter/boost)
        # Duplicated from metadata.*; top-level fields are what retrieval code
        # should filter/boost on — avoids relying on dynamic sub-object inference.
        "section_canonical": {"type": "keyword"},
        "slide_number": {"type": "integer"},
        # ---- Embedding version ---------------------------------------
        "embedding_model": {"type": "keyword"},
        # ---- Indexing timestamp — when this chunk was written to the index ---
        "indexed_at": {"type": "date"},
        # ---- Dynamic per-table metadata (section_title, entry_index, …)
        "metadata": {"type": "object", "dynamic": True},
    }
}

# ---------------------------------------------------------------------------
# Quantized variant — faiss on-disk quantization
# ---------------------------------------------------------------------------
# The d.quals HNSW graph (~1.3M child vectors, ~6 GB at fp32) far exceeds a
# small node's page cache, so its vectors are stored quantized: search walks
# the compressed graph in RAM, then rescores top candidates against the
# full-precision vectors kept on disk.  Requires OpenSearch 2.17+.
# The small indexes (profiles, knowledge library) stay full-precision — they
# fit in RAM as-is and would take the recall hit for no benefit.
#
# Compression level trade-off on t3.medium (~1.5 GiB page cache, shared with
# BM25): 8x → ~0.9 GB resident (chosen: best recall margin that still fits);
# 16x → ~550 MB (fallback if hybrid-query latency is poor); 32x → ~360 MB.

QUANTIZED_INDEX_PREFIX: str = "mcp-d-quals"
QUANTIZED_COMPRESSION_LEVEL: str = "8x"

# Per-index level overrides — candidate indexes built for side-by-side
# evaluation on the small node (only one is ever live-queried at a time).
QUANTIZED_LEVEL_OVERRIDES: dict[str, str] = {
    "mcp-d-quals-16x": "16x",
}

_QUANTIZED_EMBEDDING_FIELD: dict[str, Any] = {
    "type": "knn_vector",
    "dimension": EMBEDDING_DIMS,
    "mode": "on_disk",
    "compression_level": QUANTIZED_COMPRESSION_LEVEL,
    "space_type": "cosinesimil",
}


def index_create_body(index_name: str) -> dict[str, Any]:
    """Full ``indices.create`` body for *index_name*.

    d.quals indexes swap the embedding field for the quantized variant and
    drop the ``text.keyword`` sub-field (unqueried, and 2 KB per doc of disk).
    """
    body: dict[str, Any] = copy.deepcopy(
        {"settings": INDEX_SETTINGS, "mappings": INDEX_MAPPING}
    )
    if index_name.startswith(QUANTIZED_INDEX_PREFIX):
        props = body["mappings"]["properties"]
        props["embedding"] = copy.deepcopy(_QUANTIZED_EMBEDDING_FIELD)
        props["embedding"]["compression_level"] = QUANTIZED_LEVEL_OVERRIDES.get(
            index_name, QUANTIZED_COMPRESSION_LEVEL
        )
        props["text"].pop("fields", None)
    return body
