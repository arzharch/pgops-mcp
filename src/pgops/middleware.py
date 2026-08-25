"""Per-tool scope enforcement, and caller identity for the audit log.

`TOOL_SCOPES` in auth.py says which scope each tool needs. Until this module existed,
that mapping was **documentation, not enforcement**: `JWTVerifier(required_scopes=...)`
checks scopes once per request, against a single server-wide list. Setting that list to
`pgops:read` — the minimum any caller needs — meant every valid token cleared the only
gate there was, so a token issued with `pgops:read` alone could call `query.write`.
Verified before this was written: a read-only token ran `CREATE TABLE` successfully.

The lesson is worth stating plainly because it generalises: authentication answered
"is this caller real", and nothing answered "is this caller allowed to do *this*".
A scope table that is never consulted reads exactly like a scope table that is.

FastMCP has no per-tool scope declaration, so enforcement lives in middleware, which
gets two hooks that matter:

- `on_call_tool` — the actual gate. Deny-by-default: a tool absent from `TOOL_SCOPES`
  requires `pgops:admin`, so a tool added later without a scope entry is locked down
  rather than silently public.
- `on_list_tools` — hides tools the caller could not call anyway. This is *not* the
  security boundary (a caller can always invoke a tool it was not shown; `on_call_tool`
  is what stops it). It exists because an agent shown `query.write` will eventually try
  it, spend a turn, and get an error it cannot act on. Not offering the tool is both
  cheaper and less confusing than refusing it.

Under stdio there is no token at all. That is not a failure: the server is a subprocess
the user spawned, holding their own DSN, and there is no second principal to distinguish
(ADR-002). So an absent token means full local privilege, and the audit log records the
caller as `local`. The distinction only becomes meaningful when a port is open, which is
exactly when a token is mandatory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware, MiddlewareContext

from pgops.auth import TOOL_SCOPES, Scope

logger = logging.getLogger("pgops.middleware")

LOCAL_ACTOR = "local"


@dataclass(slots=True, frozen=True)
class Caller:
    """Who is making this call. `subject` is the token's `sub` claim, or `local`."""

    subject: str
    scopes: frozenset[str]
    authenticated: bool

    def may_call(self, tool: str) -> bool:
        # Deny by default: an unmapped tool is treated as admin-only.
        required = TOOL_SCOPES.get(tool, Scope.ADMIN)
        return not self.authenticated or required.value in self.scopes


LOCAL_CALLER = Caller(subject=LOCAL_ACTOR, scopes=frozenset(), authenticated=False)


def current_caller() -> Caller:
    """Resolve the caller from the active request, if there is one.

    Returns the local caller outside a request context (direct function calls in tests)
    and under stdio, where no token exists by design.
    """
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:  # noqa: BLE001 - no request context is a normal condition
        return LOCAL_CALLER
    if token is None:
        return LOCAL_CALLER
    return Caller(
        subject=token.subject or token.client_id or "unknown",
        scopes=frozenset(token.scopes or ()),
        authenticated=True,
    )


class ScopeEnforcement(Middleware):
    """Rejects tool calls the caller's token does not carry the scope for."""

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        tool = context.message.name
        caller = current_caller()
        if not caller.may_call(tool):
            required = TOOL_SCOPES.get(tool, Scope.ADMIN)
            # A refusal here is a permission failure, not a hint to retry differently,
            # so it names the missing scope and how to obtain one. The agent cannot
            # mint a token, which is the point — this has to reach a human.
            logger.warning(
                "denied %s for subject=%s (needs %s, has %s)",
                tool,
                caller.subject,
                required.value,
                sorted(caller.scopes),
            )
            raise ToolError(
                f"token for {caller.subject!r} lacks the {required.value} scope "
                f"required by {tool}. Re-issue with: "
                f"pgops-mcp issue-token --subject {caller.subject} --scope {required.value}"
            )
        return await call_next(context)

    async def on_list_tools(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        tools = await call_next(context)
        caller = current_caller()
        return [t for t in tools if caller.may_call(t.name)]


class ObservabilityMiddleware(Middleware):
    """Outermost span + metric for every tool call, *including* ones that never reach
    a tool body.

    Why this exists: ScopeEnforcement runs before the tool runs, so a denied call
    produced a log line and nothing else — invisible to dashboards. But permission
    denials are among the most operationally interesting signals an MCP server emits:
    a spike usually means a misconfigured agent, a token that rotated without its
    scopes being re-issued, or something probing what it can reach.

    Verdict taxonomy (deliberately four values, not two):
      - executed — tool ran and returned success
      - refused  — the tool itself said no (PgopsError: bad SQL, missing confirmation)
      - denied   — authorization said no before the tool ran (scope failure)
      - failed   — unexpected exception (bug or infrastructure)

    "The tool refused" and "you may not ask" are different incidents with different
    responders; collapsing them into one bucket would hide exactly which layer is
    rejecting traffic.

    Ordering matters: this must be registered BEFORE ScopeEnforcement so its span wraps
    the scope check too. Metrics are emitted here only when the inner boundary never
    ran (denials); otherwise the boundary's span owns them — no double counting.
    """

    async def on_call_tool(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        from pgops.observability import ToolSpan

        tool = context.message.name
        caller = current_caller()
        with ToolSpan(tool, emit_metrics=False) as span:
            span.set_caller(caller.subject)
            try:
                return await call_next(context)
            except ToolError:
                # ToolError here is either a scope denial (ScopeEnforcement below us)
                # or a refusal surfaced by the framework. Distinguish by checking
                # whether the caller was allowed at all.
                if not caller.may_call(tool):
                    span.set_verdict("denied")
                    from pgops.observability import record_denial

                    record_denial(tool, caller.subject)
                else:
                    span.set_verdict("refused")
                raise
            # Unexpected exceptions propagate unchanged — FastMCP renders them; the
            # span still records the failure verdict on the way out.

    async def on_list_tools(self, context: MiddlewareContext[Any], call_next: Any) -> Any:
        return await call_next(context)
