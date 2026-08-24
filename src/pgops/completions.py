"""Argument autocompletion for resource templates and prompts.

MCP `completion/complete` is what turns `pgops://schema/{table}` from a URI you have to
already know into one a client can offer a picker for. Without it a user has to call
`schema.inspect` first just to learn what to type — which is the kind of friction that
makes a capability go unused.

Two things about this are worth being explicit about:

**It fires per keystroke.** A client sends a completion request as the user types, so a
naive implementation issues a catalog query per character against the database it is
supposed to be taking care of. Table names change on the timescale of migrations, not
keystrokes, so the list is cached for a few seconds — long enough to collapse a burst of
typing into one query, short enough that a table created a moment ago shows up. Failure
is silent and returns nothing: an autocomplete that raises is worse than one that is
empty, because the empty one still lets the user type the name.

**FastMCP has no high-level decorator for this**, so registration goes through the
underlying low-level MCP server (`_mcp_server.completion()`). That is a private
attribute, and using it is a deliberate, isolated trade-off: the alternative is not
supporting a protocol feature at all. It is confined to this module so there is exactly
one place to fix if the attribute moves.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pgops.connections import ConnectionManager

logger = logging.getLogger("pgops.completions")

# Long enough to collapse a burst of typing into one catalog query; short enough that a
# table created seconds ago is offered.
CACHE_TTL_S = 5.0

# A completion response is a picker, not a data dump. The MCP spec caps a response at
# 100 values, and a client cannot usefully render more than a screenful anyway.
MAX_VALUES = 100

_TABLE_QUERY = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY c.relname
"""


class TableNameCache:
    """Table names for autocomplete, refreshed at most every CACHE_TTL_S."""

    def __init__(self, conn_manager: ConnectionManager, ttl_s: float = CACHE_TTL_S) -> None:
        self._conn_manager = conn_manager
        self._ttl_s = ttl_s
        self._names: list[str] = []
        self._fetched_at = 0.0

    async def names(self) -> list[str]:
        now = time.monotonic()
        if self._names and now - self._fetched_at < self._ttl_s:
            return self._names
        try:
            async with self._conn_manager.acquire_readonly() as conn:
                rows = await conn.fetch(_TABLE_QUERY)
        except Exception:  # autocomplete must never surface an error to the user
            # Serve whatever is cached. A stale picker beats a broken one, and the user
            # can always type the name themselves.
            logger.debug("completion lookup failed", exc_info=True)
            return self._names
        self._names = [r["relname"] for r in rows]
        self._fetched_at = now
        return self._names


def filter_prefix(names: list[str], prefix: str) -> list[str]:
    """Case-insensitive prefix match, with prefix matches ranked above substring ones.

    Substring matching is included because a user who types `orders` should still find
    `archived_orders` — the common naming convention puts the meaningful word last. But
    it ranks second, so an exact prefix is never buried under it.
    """
    if not prefix:
        return names[:MAX_VALUES]
    lowered = prefix.lower()
    starts = [n for n in names if n.lower().startswith(lowered)]
    contains = [n for n in names if lowered in n.lower() and n not in starts]
    return (starts + contains)[:MAX_VALUES]


def register_completions(mcp: Any, conn_manager: ConnectionManager) -> None:
    """Wire a completion handler onto the low-level MCP server.

    Registering also flips the server's advertised `completions` capability, so clients
    that check before asking will now ask.
    """
    from mcp import types

    cache = TableNameCache(conn_manager)

    # Private attribute: FastMCP exposes no high-level completion decorator, and the
    # alternative is not supporting the protocol feature at all. The decorator is
    # untyped, so mypy cannot see through it — annotated here rather than silenced
    # globally, keeping the escape hatch to this one line.
    @mcp._mcp_server.completion()  # type: ignore[untyped-decorator]
    async def complete(
        ref: Any, argument: types.CompletionArgument, context: Any = None
    ) -> types.Completion | None:
        # Only the {table} argument has a meaningful answer. Returning None elsewhere is
        # correct rather than lazy: offering guesses for a free-text `sql` or
        # `description` argument would be noise a client has to render.
        if getattr(argument, "name", None) != "table":
            return None
        values = filter_prefix(await cache.names(), argument.value or "")
        return types.Completion(values=values, total=len(values), hasMore=False)
