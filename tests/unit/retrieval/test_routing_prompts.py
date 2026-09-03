"""Routing-prompt unit tests for the Dalberg Retrieval MCP server."""

from __future__ import annotations

import asyncio

from retrieval.mcp.routing_prompts import (
    MCP_SERVER_INSTRUCTIONS,
    MCP_SERVER_NAME,
    TOOL_DOC_PREFIX,
    _mcp_tool_doc,
)
from retrieval.mcp.server import build_mcp


def test_server_name_is_dalberg_retrieval() -> None:
    assert MCP_SERVER_NAME == "Dalberg Retrieval"


def test_instructions_routing_block_present() -> None:
    text = MCP_SERVER_INSTRUCTIONS
    assert "MCP SERVER ROUTING" in text
    assert "Dalberg Retrieval" in text
    assert "MULTIPLE MCP servers" in text
    assert "Do NOT use this server" in text
    assert "CITATIONS" in text and "citation_url" in text
    assert "RETRIEVAL STRATEGY" in text
    # Routing block must reference list_sources() so adding new sources
    # does not require a prompt change.
    assert "list_sources()" in text
    # retrieval_planner is the mandatory first step; must be documented.
    assert "retrieval_planner" in text
    # Enrichment step guidance must be present (quality-critical).
    assert "enrichment" in text.lower()
    # Fallback / recovery guidance must be present.
    assert "FALLBACK" in text or "fallback" in text.lower()


def test_mcp_tool_doc_prefixes_first_line() -> None:
    out = _mcp_tool_doc("First line summary.\n\nMore details.")
    assert out.startswith(TOOL_DOC_PREFIX)
    assert out.splitlines()[0] == TOOL_DOC_PREFIX + "First line summary."


def test_mcp_tool_doc_single_line() -> None:
    out = _mcp_tool_doc("Single line.")
    assert out == TOOL_DOC_PREFIX + "Single line."


def test_build_mcp_uses_dalberg_retrieval_name() -> None:
    mcp = build_mcp()
    assert mcp.name == "Dalberg Retrieval"


def test_tool_descriptions_have_prefix() -> None:
    mcp = build_mcp()
    tools = asyncio.run(mcp.list_tools())
    assert tools, "FastMCP returned no registered tools"
    expected = {"list_sources", "get_schema", "search", "retrieval_planner", "semantic_search", "airtable_lookup"}
    seen = {t.name for t in tools}
    assert expected <= seen, f"Missing tools: {expected - seen}"
    for tool in tools:
        desc = tool.description or ""
        assert desc.startswith(TOOL_DOC_PREFIX), (
            f"Tool {tool.name!r} description missing prefix: {desc[:80]!r}"
        )
