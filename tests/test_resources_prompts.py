"""MCP resources and prompts — the two server primitives beyond tools.

Read through the real FastMCP server object, since resource URIs, templates and prompt
names are a public contract in exactly the way tool names are.
"""

from __future__ import annotations

import json

import pytest

from pgops.audit import AuditEntry, AuditLog
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager
from pgops.__main__ import build_server

DOCUMENTED_RESOURCES = {
    "pgops://schema",
    "pgops://schema/summary",
    "pgops://health",
    "pgops://migrations",
    "pgops://audit/recent",
    "pgops://config",
}

DOCUMENTED_PROMPTS = {
    "diagnose-slow-query",
    "plan-safe-migration",
    "incident-triage",
    "review-index-health",
    "explain-safety-model",
}


def _body(result: object) -> str:
    return result.contents[0].content  # type: ignore[attr-defined]


async def test_documented_resources_are_registered(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = build_server(config, conn_manager)
    uris = {str(r.uri) for r in await server.list_resources()}
    assert DOCUMENTED_RESOURCES <= uris, uris


async def test_resource_template_is_registered(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = build_server(config, conn_manager)
    templates = {t.uri_template for t in await server.list_resource_templates()}
    assert "pgops://schema/{table}" in templates


async def test_documented_prompts_are_registered(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = build_server(config, conn_manager)
    names = {p.name for p in await server.list_prompts()}
    assert DOCUMENTED_PROMPTS <= names, names


@pytest.mark.parametrize("uri", sorted(DOCUMENTED_RESOURCES))
async def test_every_resource_returns_valid_json(
    conn_manager: ConnectionManager, config: PgopsConfig, uri: str
) -> None:
    server = build_server(config, conn_manager)
    json.loads(_body(await server.read_resource(uri)))


async def test_schema_template_scopes_to_one_table(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = build_server(config, conn_manager)
    data = json.loads(_body(await server.read_resource("pgops://schema/items")))
    assert [t["name"] for t in data["tables"]] == ["items"]
    assert {c["column_name"] for c in data["tables"][0]["columns"]} == {"id", "name"}


async def test_summary_resource_is_cheaper_than_full(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """A wide database's full catalog is large, and a client attaching context to every
    message should be able to pick the cheap version."""
    server = build_server(config, conn_manager)
    full = _body(await server.read_resource("pgops://schema"))
    summary = _body(await server.read_resource("pgops://schema/summary"))
    assert len(summary) < len(full)
    assert "columns" not in json.loads(summary)["tables"][0]


async def test_config_resource_never_exposes_the_dsn(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """The DSN carries the password. A resource is the *most* likely thing to be
    auto-attached to model context, so this must never carry credentials."""
    server = build_server(config, conn_manager)
    body = _body(await server.read_resource("pgops://config"))
    assert "dsn" not in body.lower() or "omitted" in body
    assert "pgops_test:pgops_test" not in body
    assert config.dsn not in body


async def test_audit_resource_omits_sql_text(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """The audit log on disk keeps full statements on purpose — an incident review needs
    them. Exposing that same text as a resource is a different risk: executed SQL embeds
    literal values (an email in a WHERE clause, an amount in an UPDATE), and a resource
    may be auto-attached to model context. Metadata is enough to see what happened.
    """
    log = AuditLog(config.audit_path)
    log.record(
        AuditEntry(
            tool="query.write",
            sql="DELETE FROM users WHERE email = 'someone@example.com'",
            verdict="executed",
            classification="write",
            rows_affected=1,
        )
    )
    server = build_server(config, conn_manager)
    body = _body(await server.read_resource("pgops://audit/recent"))

    assert "someone@example.com" not in body
    assert "DELETE FROM users" not in body
    # ...but the event itself is still visible and correlatable
    entry = json.loads(body)["entries"][-1]
    assert entry["verdict"] == "executed"
    assert entry["tool"] == "query.write"
    assert len(entry["sql_sha256"]) == 64


async def test_health_resource_matches_the_tool(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """Resources must mirror tools, not diverge from them — two sources of truth for
    the same question is how a client gets contradictory answers."""
    server = build_server(config, conn_manager)
    resource = json.loads(_body(await server.read_resource("pgops://health")))
    tool = (await server.call_tool("db.health", {})).structured_content
    assert {f["category"] for f in resource["findings"]} == {
        f["category"] for f in tool["findings"]
    }


async def test_prompts_render_with_arguments(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = build_server(config, conn_manager)
    rendered = await server.render_prompt(
        "diagnose-slow-query", {"sql": "SELECT * FROM orders WHERE x = 1"}
    )
    text = rendered.messages[0].content.text  # type: ignore[union-attr]
    assert "SELECT * FROM orders WHERE x = 1" in text
    assert "query.explain" in text


async def test_argumentless_prompts_render(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = build_server(config, conn_manager)
    for name in ["incident-triage", "review-index-health", "explain-safety-model"]:
        rendered = await server.render_prompt(name, {})
        assert rendered.messages[0].content.text.strip()  # type: ignore[union-attr]


async def test_prompts_steer_toward_the_safe_path(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """Prompts exist to encode operational judgment the tools cannot express. If they
    stop carrying the safety guidance they are just documentation.

    Whitespace is normalized before matching: these assertions are about what the prompt
    *says*, and re-wrapping a paragraph should not fail a test about its meaning.
    """
    server = build_server(config, conn_manager)

    async def rendered(name: str, args: dict[str, str]) -> str:
        result = await server.render_prompt(name, args)
        return " ".join(result.messages[0].content.text.split())  # type: ignore[union-attr]

    migration = await rendered("plan-safe-migration", {"description": "add a column"})
    assert "allow_drops" in migration
    assert "safe_alternative" in migration
    assert "Do not call `migration.apply`" in migration

    indexes = await rendered("review-index-health", {})
    assert "stats_window" in indexes
    assert "Do not recommend dropping an index on a short window" in indexes

    triage = await rendered("incident-triage", {})
    assert "Do not kill anything without explicit approval" in triage
