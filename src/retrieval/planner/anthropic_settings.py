"""Anthropic API settings for the NL planner.

Lifted unchanged from :mod:`pipeline.api.anthropic_query_settings` (kept for
back-compat re-export). Only the docstring is updated to reflect that
Claude is now used by the source-agnostic ``search`` MCP tool, not the
old ``query_dalberg_profiles``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_ENV = _REPO_ROOT / ".env"


class AnthropicQuerySettings(BaseSettings):
    """Claude API used by the NL planner and the answer synthesizer.

    Used by the ``search`` MCP tool only. The primitive tools
    (``semantic_search``, ``airtable_lookup``) never call Claude.
    """

    model_config = SettingsConfigDict(
        env_file=(_REPO_ENV, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_key: str = Field(
        ...,
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"),
    )
    model: str = Field(
        default="claude-sonnet-4-6",
        validation_alias=AliasChoices("ANTHROPIC_MODEL", "CLAUDE_MODEL"),
    )
    max_output_tokens: int = Field(
        default=2048,
        validation_alias=AliasChoices("ANTHROPIC_MAX_OUTPUT_TOKENS"),
    )
    nl_readable_answer: bool = Field(
        default=True,
        validation_alias=AliasChoices("ANTHROPIC_NL_READABLE_ANSWER"),
    )


@lru_cache
def get_anthropic_query_settings() -> AnthropicQuerySettings:
    return AnthropicQuerySettings()
