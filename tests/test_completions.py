"""Autocomplete for the {table} argument of pgops://schema/{table}.

Without completion a user has to call `schema.inspect` first just to learn what to type
into a resource template, which is the kind of friction that leaves a capability unused.

The operational hazard is that completion fires *per keystroke*: a naive handler issues
a catalog query per character against the database it is meant to be taking care of.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from pgops.completions import MAX_VALUES, TableNameCache, filter_prefix


def test_empty_prefix_offers_everything() -> None:
    assert filter_prefix(["items", "orders"], "") == ["items", "orders"]


def test_prefix_match_is_case_insensitive() -> None:
    assert filter_prefix(["Orders", "items"], "ord") == ["Orders"]


def test_prefix_matches_rank_above_substring_matches() -> None:
    """A user typing `orders` should still find `archived_orders` — the usual naming
    convention puts the meaningful word last — but an exact prefix must not be buried
    underneath it."""
    names = ["archived_orders", "orders", "order_items"]
    assert filter_prefix(names, "orders") == ["orders", "archived_orders"]


def test_no_duplicates_when_a_name_matches_both_ways() -> None:
    assert filter_prefix(["orders"], "orders") == ["orders"]


def test_response_is_capped() -> None:
    """A completion response is a picker, not a data dump; the MCP spec caps it at 100."""
    assert len(filter_prefix([f"t{i}" for i in range(500)], "t")) == MAX_VALUES


# --- caching --------------------------------------------------------------------------


class CountingManager:
    """Stands in for the connection manager, counting catalog round trips."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.queries = 0

    def acquire_readonly(self) -> Any:
        manager = self

        class _Conn:
            async def fetch(self, _sql: str) -> list[dict[str, str]]:
                manager.queries += 1
                return [{"relname": n} for n in manager.names]

        class _Ctx:
            async def __aenter__(self) -> _Conn:
                return _Conn()

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Ctx()


async def test_typing_a_name_does_not_issue_a_query_per_keystroke() -> None:
    """The reason the cache exists. Ten characters of typing must not become ten
    catalog queries against a database under load."""
    manager = CountingManager(["items", "orders"])
    cache = TableNameCache(manager, ttl_s=60)  # type: ignore[arg-type]
    for _ in range(10):
        assert await cache.names() == ["items", "orders"]
    assert manager.queries == 1


async def test_cache_expires_so_a_new_table_shows_up() -> None:
    """Table names change on the timescale of migrations, so the TTL is short enough
    that a table created a moment ago is offered."""
    manager = CountingManager(["items"])
    cache = TableNameCache(manager, ttl_s=0.01)  # type: ignore[arg-type]
    assert await cache.names() == ["items"]
    manager.names = ["items", "brand_new"]
    await asyncio.sleep(0.05)
    assert await cache.names() == ["items", "brand_new"]


async def test_a_database_failure_serves_stale_names_instead_of_raising() -> None:
    """An autocomplete that raises is worse than one that is stale: the stale one still
    lets the user pick, and the error would surface as a broken client UI."""

    class BreakingManager(CountingManager):
        def acquire_readonly(self) -> Any:
            raise RuntimeError("pool exhausted")

    manager = BreakingManager(["items"])
    cache = TableNameCache(manager, ttl_s=0.01)  # type: ignore[arg-type]
    assert await cache.names() == []  # nothing cached yet: empty, not an exception

    warm = TableNameCache(CountingManager(["items"]), ttl_s=0.01)  # type: ignore[arg-type]
    assert await warm.names() == ["items"]
    warm._conn_manager = manager  # type: ignore[attr-defined]
    await asyncio.sleep(0.05)
    assert await warm.names() == ["items"]


# --- live protocol round trip ---------------------------------------------------------


@pytest.mark.slow
async def test_client_gets_table_completions_over_the_protocol(
    conn_manager: object, config: object
) -> None:
    """End to end through completion/complete — the capability has to be advertised, not
    just implemented, or a client never asks."""
    import mcp.types
    from fastmcp import Client

    from pgops.__main__ import build_server

    server = build_server(config, conn_manager)  # type: ignore[arg-type]
    async with Client(server) as client:
        result = await client.complete(
            mcp.types.ResourceTemplateReference(type="ref/resource", uri="pgops://schema/{table}"),
            {"name": "table", "value": "it"},
        )
        assert "items" in result.values

        empty = await client.complete(
            mcp.types.ResourceTemplateReference(type="ref/resource", uri="pgops://schema/{table}"),
            {"name": "table", "value": "zzz_no_such_table"},
        )
        assert empty.values == []
