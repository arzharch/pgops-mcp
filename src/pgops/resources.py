"""MCP resources — read-only state addressable by URI.

Tools and resources answer different questions, and shipping only tools (as this project
did through Phase 5) leaves a real gap:

- A **tool** is model-controlled. The agent decides to call it, and every call costs a
  turn and a round trip.
- A **resource** is application-controlled. The *client* can attach it as context up
  front, pin it in a sidebar, or refresh it — without the model deciding to spend a turn.

The practical difference for this server: "what does my schema look like" is background
context for almost every database conversation. As a tool it costs a turn each time the
model wants it. As a resource, a client can attach `pgops://schema` once and have it
present for the whole session.

Everything here is strictly read-only and mirrors data already reachable through tools —
resources add no new capability and therefore no new attack surface. The audit resource
is the one to be careful with: it exposes executed SQL, so it deliberately returns only
recent *metadata* (verdicts, timings, hashes) rather than full statement text, which can
embed literal values from the rows being operated on.
"""

from __future__ import annotations

import json
from typing import Any

from pgops.audit import AuditLog
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.tools.health import db_health
from pgops.tools.migrations import migration_history
from pgops.tools.schema import schema_inspect


async def schema_resource(conn_manager: ConnectionManager) -> str:
    snapshot = await schema_inspect(conn_manager, level="full")
    return json.dumps(snapshot.to_dict("full"), indent=2)


async def schema_summary_resource(conn_manager: ConnectionManager) -> str:
    """Table names, row estimates and sizes only.

    Exists alongside the full schema because a wide database's full catalog dump is
    large, and a client attaching context to every message should be able to choose the
    cheap version.
    """
    snapshot = await schema_inspect(conn_manager, level="summary")
    return json.dumps(snapshot.to_dict("summary"), indent=2)


async def table_resource(conn_manager: ConnectionManager, table: str) -> str:
    """Resource *template*: `pgops://schema/{table}`.

    Lets a client pull one table's definition without the whole catalog — the common
    case when a conversation is about a specific table.
    """
    snapshot = await schema_inspect(conn_manager, level="full", table=table)
    return json.dumps(snapshot.to_dict("full"), indent=2)


async def health_resource(conn_manager: ConnectionManager) -> str:
    report = await db_health(conn_manager)
    return json.dumps(report.to_dict(), indent=2)


async def migrations_resource(conn_manager: ConnectionManager) -> str:
    return json.dumps(await migration_history(conn_manager, limit=20), indent=2)


def audit_resource(config: PgopsConfig, limit: int = 50) -> str:
    """Recent audit activity — metadata only, never full SQL text.

    The audit log on disk keeps complete statements on purpose (an incident review needs
    them). Exposing that same text as a resource is a different risk: a client may attach
    a resource to model context automatically, and executed SQL embeds literal values —
    the email address in a WHERE clause, the amount in an UPDATE. So this view carries
    the verdict, timing, tool and SQL *hash*, which is enough to see what happened and to
    correlate with the full log, without piping row data into a model's context by
    default.
    """
    log = AuditLog(config.audit_path)
    entries = log.read_all()[-limit:]
    redacted = [
        {
            "ts": e.get("ts"),
            "audit_id": e.get("audit_id"),
            # Identity is the point of the log on a multi-caller deployment, and unlike
            # SQL text it carries no row data, so it survives redaction.
            "actor": e.get("actor"),
            "tool": e.get("tool"),
            "verdict": e.get("verdict"),
            "classification": e.get("classification"),
            "sql_sha256": e.get("sql_sha256"),
            "duration_ms": e.get("duration_ms"),
            "rows_affected": e.get("rows_affected"),
            "error_code": e.get("error_code"),
        }
        for e in entries
    ]
    return json.dumps(
        {
            "entries": redacted,
            "note": (
                "SQL text is omitted here because executed statements embed literal "
                f"values; the full log is at {config.audit_path}"
            ),
        },
        indent=2,
    )


def config_resource(config: PgopsConfig) -> str:
    """The server's effective safety configuration.

    Useful to an operator asking "what is this server actually allowed to do right now",
    and answerable without granting any new capability. The DSN is **not** included — it
    contains a password.
    """
    payload: dict[str, Any] = {
        "read_only": config.read_only,
        "approval_mode": config.approval_mode,
        "audit_log_path": str(config.audit_path),
        "confirm_token_ttl_s": config.confirm_token_ttl_s,
        "timeouts": {
            "default_ms": config.timeouts.default_ms,
            "max_ms": config.timeouts.max_ms,
        },
        "row_limits": {"default": config.row_limits.default, "max": config.row_limits.max},
        "pools": {
            "readonly_max": config.pools.readonly_max,
            "readwrite_max": config.pools.readwrite_max,
            "acquire_timeout_s": config.pools.acquire_timeout_s,
        },
        "note": "the DSN is deliberately omitted — it contains credentials",
    }
    return json.dumps(payload, indent=2)
