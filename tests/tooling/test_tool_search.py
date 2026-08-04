from __future__ import annotations

from prodagent.core.types import ToolMeta
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.search import (
    ToolDescriptionIndex,
    ToolNameParser,
    ToolSearchIndex,
)


def _tool(name: str, description: str = "", domain: str = "") -> FunctionTool:
    async def _noop(**_: object) -> dict:
        return {}

    return FunctionTool(
        name=name,
        fn=_noop,
        meta=ToolMeta(name=name, domain=domain) if domain else None,
        schema={"name": name, "description": description, "input_schema": {"type": "object"}},
    )


def test_parser_snake_case():
    p = ToolNameParser.parse("get_user_email")
    assert p.parts == ["get", "user", "email"]


def test_parser_reserved_prefix():
    p = ToolNameParser.parse("mcp__github__create_issue")
    assert p.parts == ["mcp", "github", "create", "issue"]


def test_parser_single_word():
    p = ToolNameParser.parse("ping")
    assert p.parts == ["ping"]


def _cfg():
    from prodagent.tooling.search import ToolSearchConfig

    return ToolSearchConfig()


def test_description_word_boundary_match():
    tools = [_tool("search", "Search the web for a query")]
    idx = ToolDescriptionIndex(tools)
    indexed = tools[0]
    assert idx.score(indexed, ["web"], _cfg()) > 0


def test_description_word_boundary_scores_proportionally():
    tools = [_tool("search", "Search the web for a query")]
    from prodagent.tooling.search import ToolSearchConfig

    idx = ToolDescriptionIndex(tools)
    cfg = ToolSearchConfig()
    indexed = tools[0]
    assert idx.score(indexed, ["web"], cfg) > 0
    assert idx.score(indexed, ["web", "query"], cfg) > idx.score(indexed, ["web"], cfg)


def test_search_exact_name_returns_single():
    tools = [_tool("get_user"), _tool("get_email"), _tool("send_email")]
    idx = ToolSearchIndex(tools)
    results = idx.search("get_user")
    assert len(results) == 1
    assert results[0].name == "get_user"


def test_search_partial_name_ranks_contains_above_fallback():
    tools = [
        _tool("fetch_user_profile"),
        _tool("delete_account"),
        _tool("list_orders"),
    ]
    idx = ToolSearchIndex(tools)
    results = idx.search("user")
    assert results
    assert results[0].name == "fetch_user_profile"


def test_search_camel_case_acronym_via_parts():
    tools = [_tool("GetXMLNode"), _tool("send_email"), _tool("list_orders")]
    idx = ToolSearchIndex(tools)
    results = idx.search("xml")
    assert results
    assert results[0].name == "GetXMLNode"


def test_search_description_word_boundary():
    tools = [
        _tool("lookup", "Resolve a user's profile from the directory"),
        _tool("send", "Send an email message"),
    ]
    idx = ToolSearchIndex(tools)
    results = idx.search("email")
    assert results
    assert results[0].name == "send"


def test_search_empty_query_returns_nothing():
    idx = ToolSearchIndex([_tool("a"), _tool("b")])
    assert idx.search("") == []


def test_search_no_tools_returns_nothing():
    idx = ToolSearchIndex([])
    assert idx.search("anything") == []


def test_search_caps_results_at_max():
    tools = [_tool(f"get_user_{i}") for i in range(10)]
    idx = ToolSearchIndex(tools)
    results = idx.search("user", max_results=3)
    assert len(results) == 3


def test_search_domain_tag_match_boosts():
    from prodagent.tooling.search import ToolSearchConfig

    tools = [
        _tool("query", "Run a query", domain="payments"),
        _tool("send", "Send something", domain="notifications"),
    ]
    idx = ToolSearchIndex(tools, config=ToolSearchConfig())
    results = idx.search("payments")
    assert results
    assert results[0].name == "query"
