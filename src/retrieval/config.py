"""Source registry — loads ``config/retrieval_sources.yaml`` and instantiates
per-source Airtable + OpenSearch adapters.

Adding a logical source is config-only:
    1. Add an entry under ``sources:`` in the YAML.
    2. Set ``enabled: true``.
    3. Provide its Airtable ``base_id`` / ``table_name`` and OpenSearch
       ``index_name``.

No code change required for new sources of the same shape.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from retrieval.models import (
    Hint,
    RetrievalQuery,
    SchemaDescriptor,
    SearchResult,
)
from retrieval.paths import DEFAULT_RETRIEVAL_SOURCES_PATH

log = structlog.get_logger(__name__)

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


# ---------------------------------------------------------------------------
# Raw config dataclasses (parsed from YAML, no I/O performed yet)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    provider: str = "voyage"
    model: str = "voyage-4"
    dims: int = 1024
    query_prefix: str = "query: "
    batch_size: int = 32


@dataclass(frozen=True, slots=True)
class RankingConfig:
    fusion: str = "rrf"
    rrf_k: int = 60
    reranker_enabled: bool = False
    reranker_model: str = "rerank-2.5"


@dataclass(frozen=True, slots=True)
class AirtableSourceConfig:
    base_id: str
    table_name: str
    schema_snapshot_path: Path
    long_text_fields: tuple[str, ...] = ()
    long_text_truncate: int = 800


@dataclass(frozen=True, slots=True)
class OpenSearchSourceConfig:
    index_name: str
    k: int = 10
    over_fetch_k: int = 50    # candidates retrieved per method before person-dedup+truncation
    search_mode: str = "hybrid"           # hybrid | knn | bm25
    # When True, extract structured facet filters from the NL query (grounded in
    # the index's actual facet values) and apply them as filters on the search.
    facet_filtering: bool = False
    # How NL-derived facets are applied (spec 007). Caller-supplied filters and
    # confidentiality are hard in every mode.
    #   hard:     exclude non-matching docs (legacy behavior)
    #   soft:     derived facets only boost ranking, never exclude
    #   fallback: hard first; if fewer than facet_fallback_min_results return,
    #             re-run with derived facets as boosts instead of filters
    facet_mode: str = "hard"
    facet_fallback_min_results: int = 3


@dataclass(frozen=True, slots=True)
class SourceConfig:
    """Parsed config for one logical source (does not own runtime clients)."""

    name: str
    display_name: str
    enabled: bool
    description: str | None
    chunking_strategy: str
    identifier_field: str | None
    airtable: AirtableSourceConfig | None
    opensearch: OpenSearchSourceConfig | None
    search_mode: str
    top_k: int


@dataclass(frozen=True, slots=True)
class RetrievalConfig:
    """All parsed YAML data."""

    embedding: EmbeddingConfig
    ranking: RankingConfig
    sources: dict[str, SourceConfig]


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def _expand_env(value: Any) -> Any:
    """Recursively substitute ``${VAR}`` from process env."""

    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            var = match.group(1)
            env_val = os.environ.get(var)
            if env_val is None:
                msg = f"Required env var ${{{var}}} is not set"
                raise RuntimeError(msg)
            return env_val
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _parse_airtable(raw: dict[str, Any], default_truncate: int) -> AirtableSourceConfig:
    if not raw.get("base_id"):
        msg = "airtable.base_id is required"
        raise ValueError(msg)
    if not raw.get("table_name"):
        msg = "airtable.table_name is required"
        raise ValueError(msg)
    return AirtableSourceConfig(
        base_id=str(raw["base_id"]),
        table_name=str(raw["table_name"]),
        schema_snapshot_path=Path(raw.get("schema_snapshot_path", "")),
        long_text_fields=tuple(raw.get("long_text_fields") or ()),
        long_text_truncate=int(raw.get("long_text_truncate", default_truncate)),
    )


def _parse_opensearch(raw: dict[str, Any], default_k: int) -> OpenSearchSourceConfig:
    if not raw.get("index_name"):
        msg = "opensearch.index_name is required"
        raise ValueError(msg)
    k = int(raw.get("k", default_k))
    facet_mode = str(raw.get("facet_mode", "hard")).lower()
    if facet_mode not in ("hard", "soft", "fallback"):
        msg = f"opensearch.facet_mode must be hard|soft|fallback, got {facet_mode!r}"
        raise ValueError(msg)
    return OpenSearchSourceConfig(
        index_name=str(raw["index_name"]),
        k=k,
        over_fetch_k=int(raw.get("over_fetch_k", max(50, k * 5))),
        search_mode=str(raw.get("search_mode", "hybrid")),
        facet_filtering=bool(raw.get("facet_filtering", False)),
        facet_mode=facet_mode,
        facet_fallback_min_results=int(raw.get("facet_fallback_min_results", 3)),
    )


def parse_config(data: dict[str, Any]) -> RetrievalConfig:
    """Parse a YAML mapping into a :class:`RetrievalConfig`."""

    expanded = _expand_env(data)
    defaults = expanded.get("defaults", {}) or {}
    embed_raw = defaults.get("embedding", {}) or {}
    rank_raw = defaults.get("ranking", {}) or {}
    at_defaults = defaults.get("airtable", {}) or {}
    default_truncate = int(at_defaults.get("long_text_truncate", 800))
    default_top_k = int(defaults.get("top_k", 10))
    default_search_mode = str(defaults.get("search_mode", "hybrid"))

    embedding = EmbeddingConfig(
        provider=str(embed_raw.get("provider", "voyage")),
        model=str(embed_raw.get("model", "voyage-4")),
        dims=int(embed_raw.get("dims", 1024)),
        query_prefix=str(embed_raw.get("query_prefix", "query: ")),
        batch_size=int(embed_raw.get("batch_size", 32)),
    )
    ranking = RankingConfig(
        fusion=str(rank_raw.get("fusion", "rrf")),
        rrf_k=int(rank_raw.get("rrf_k", 60)),
        reranker_enabled=bool(rank_raw.get("reranker_enabled", False)),
        reranker_model=str(rank_raw.get("reranker_model", "rerank-2.5")),
    )

    raw_sources = expanded.get("sources", {}) or {}
    if not raw_sources:
        msg = "retrieval_sources.yaml has no `sources:` entries"
        raise ValueError(msg)

    sources: dict[str, SourceConfig] = {}
    for name, cfg in raw_sources.items():
        if not isinstance(cfg, dict):
            msg = f"source {name!r} must be a mapping"
            raise TypeError(msg)
        airtable_cfg = (
            _parse_airtable(cfg["airtable"], default_truncate)
            if cfg.get("airtable")
            else None
        )
        opensearch_cfg = (
            _parse_opensearch(cfg["opensearch"], default_top_k)
            if cfg.get("opensearch")
            else None
        )
        if airtable_cfg is None and opensearch_cfg is None:
            msg = f"source {name!r} must define at least one of airtable or opensearch"
            raise ValueError(msg)

        sources[name] = SourceConfig(
            name=name,
            display_name=str(cfg.get("display_name", name)),
            enabled=bool(cfg.get("enabled", False)),
            description=cfg.get("description"),
            chunking_strategy=str(cfg.get("chunking_strategy", "parent_child")),
            identifier_field=cfg.get("identifier_field"),
            airtable=airtable_cfg,
            opensearch=opensearch_cfg,
            search_mode=str(cfg.get("search_mode", default_search_mode)),
            top_k=int(cfg.get("top_k", default_top_k)),
        )

    return RetrievalConfig(embedding=embedding, ranking=ranking, sources=sources)


def load_config(path: str | Path | None = None) -> RetrievalConfig:
    """Load and parse the YAML at ``path`` (defaults to the repo path)."""

    target = Path(path) if path else DEFAULT_RETRIEVAL_SOURCES_PATH
    if not target.is_file():
        msg = f"retrieval_sources.yaml not found at {target}"
        raise FileNotFoundError(msg)
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return parse_config(data)


# ---------------------------------------------------------------------------
# LogicalSource — the runtime object that owns adapter instances
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LogicalSource:
    """One enabled source bundling its airtable + opensearch adapters.

    Constructed by :meth:`SourceRegistry.load` so adapter wiring lives in
    one place. Tools and the router only ever see this object, never the
    raw ``SourceConfig`` or adapter classes directly.
    """

    config: SourceConfig
    airtable: Any | None = None       # AirtableSourceProtocol
    opensearch: Any | None = None     # OpenSearchSourceProtocol

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def display_name(self) -> str:
        return self.config.display_name

    @property
    def description(self) -> str | None:
        return self.config.description

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def capabilities(self) -> list[str]:
        caps: list[str] = []
        if self.opensearch is not None:
            caps.append("semantic")
        if self.airtable is not None:
            caps.append("structured")
        return caps

    def get_schema(self) -> SchemaDescriptor:
        if self.airtable is not None:
            return self.airtable.get_schema()
        if self.opensearch is not None:
            # OpenSearch-only sources still have a known mapping; return
            # the indexer's well-known fields as a degenerate schema.
            from retrieval.sources.opensearch import opensearch_only_schema  # noqa: PLC0415

            return opensearch_only_schema(self)
        msg = f"source {self.name!r} has no adapters wired"
        raise RuntimeError(msg)

    async def query(self, q: RetrievalQuery) -> tuple[list[SearchResult], list[Hint]]:
        """Dispatch to the configured adapters according to ``q.mode``.

        ``hybrid`` runs both adapters concurrently. The router (not this
        method) is responsible for the cross-source RRF merge.
        """
        mode = q.mode
        run_at = mode in ("airtable_only", "hybrid") and self.airtable is not None
        run_os = mode in ("semantic_only", "hybrid") and self.opensearch is not None
        if not run_at and not run_os:
            return [], []

        coros = []
        if run_at:
            coros.append(
                self.airtable.filter_structured(
                    formula=q.formula,
                    fields=q.fields,
                    max_records=q.max_records,
                )
            )
        if run_os:
            coros.append(
                self.opensearch.search_semantic(
                    query=q.question or "",
                    embedding=q.embedding,
                    top_k=q.top_k,
                    filters=q.filters,
                )
            )
        results = await asyncio.gather(*coros, return_exceptions=False)

        hits: list[SearchResult] = []
        hints: list[Hint] = []
        for res_hits, res_hints in results:
            hits.extend(res_hits)
            hints.extend(res_hints)
        return hits, hints


# ---------------------------------------------------------------------------
# SourceRegistry — top-level entry point
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceRegistry:
    """In-memory registry of enabled logical sources."""

    config: RetrievalConfig
    sources: dict[str, LogicalSource] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        instantiate_adapters: bool = True,
    ) -> "SourceRegistry":
        """Load the YAML and (optionally) instantiate all enabled adapters.

        Setting ``instantiate_adapters=False`` parses config without touching
        Airtable or OpenSearch — useful for unit tests and for tools that
        only need to enumerate sources (``list_sources``, ``get_schema``).
        """
        cfg = load_config(path)
        registry = cls(config=cfg)
        if instantiate_adapters:
            registry._wire_adapters()
        else:
            for name, src_cfg in cfg.sources.items():
                if src_cfg.enabled:
                    registry.sources[name] = LogicalSource(config=src_cfg)
        return registry

    def _wire_adapters(self) -> None:
        # Imported lazily to keep ``load_config`` cheap for unit tests that
        # do not need live clients.
        from retrieval.settings import get_runtime_settings  # noqa: PLC0415
        from retrieval.sources.airtable import AirtableSource  # noqa: PLC0415
        from retrieval.sources.opensearch import OpenSearchSource  # noqa: PLC0415

        runtime = get_runtime_settings()

        for name, src_cfg in self.config.sources.items():
            if not src_cfg.enabled:
                continue
            airtable_adapter = None
            opensearch_adapter = None
            try:
                if src_cfg.airtable is not None:
                    airtable_adapter = AirtableSource(
                        name=name,
                        display_name=src_cfg.display_name,
                        cfg=src_cfg.airtable,
                        identifier_field=src_cfg.identifier_field,
                        description=src_cfg.description,
                        pat_token=runtime.airtable_pat_token,
                    )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "airtable_adapter_init_failed",
                    source=name,
                    error=str(exc),
                )
            try:
                if src_cfg.opensearch is not None:
                    opensearch_adapter = OpenSearchSource(
                        name=name,
                        display_name=src_cfg.display_name,
                        cfg=src_cfg.opensearch,
                        embedding_cfg=self.config.embedding,
                        ranking_cfg=self.config.ranking,
                        runtime=runtime,
                    )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "opensearch_adapter_init_failed",
                    source=name,
                    error=str(exc),
                )
            self.sources[name] = LogicalSource(
                config=src_cfg,
                airtable=airtable_adapter,
                opensearch=opensearch_adapter,
            )

    # ------------------------------------------------------------------
    # Public lookups
    # ------------------------------------------------------------------

    def get(self, name: str) -> LogicalSource:
        if name not in self.sources:
            msg = (
                f"Unknown or disabled source: {name!r}. "
                f"Enabled: {sorted(self.sources)}"
            )
            raise KeyError(msg)
        return self.sources[name]

    def names(self) -> list[str]:
        return sorted(self.sources)

    def enabled_sources(self) -> list[LogicalSource]:
        return [s for s in self.sources.values() if s.enabled]

    def describe_all(self) -> list[dict[str, Any]]:
        """Payload for the ``list_sources`` MCP tool."""
        out: list[dict[str, Any]] = []
        for name, src_cfg in self.config.sources.items():
            wired = self.sources.get(name)
            out.append(
                {
                    "name": name,
                    "display_name": src_cfg.display_name,
                    "description": src_cfg.description,
                    "enabled": src_cfg.enabled,
                    "capabilities": (
                        wired.capabilities() if wired else []
                    ),
                    "identifier_field": src_cfg.identifier_field,
                    "chunking_strategy": src_cfg.chunking_strategy,
                    "airtable_table": (
                        src_cfg.airtable.table_name if src_cfg.airtable else None
                    ),
                    "opensearch_index": (
                        src_cfg.opensearch.index_name if src_cfg.opensearch else None
                    ),
                }
            )
        return out


# Convenience module-level cache so tools can call ``get_registry()`` without
# threading the object through every layer. Pass ``reload=True`` to refresh.
_REGISTRY: SourceRegistry | None = None


def get_registry(
    *,
    reload: bool = False,
    path: str | Path | None = None,
    instantiate_adapters: bool = True,
) -> SourceRegistry:
    global _REGISTRY
    if _REGISTRY is None or reload:
        _REGISTRY = SourceRegistry.load(
            path=path,
            instantiate_adapters=instantiate_adapters,
        )
    return _REGISTRY
