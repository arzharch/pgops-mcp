"""End-to-end tests through the real FastMCP server object.

These exist because unit-testing the tool functions directly missed two production
bugs: `schema.inspect(level="full")` returned raw `bytes` from a catalog column and
`db.health` returned a `Decimal`, both of which are perfectly fine Python objects and
both of which fail when the MCP layer encodes the result as JSON. Calling through
`server.call_tool` exercises the serialization boundary an agent actually hits.
"""

from __future__ import annotations

import json

from fastmcp import FastMCP

from pgops.__main__ import build_server
from pgops.config import PgopsConfig
from pgops.connections import ConnectionManager

DOCUMENTED_TOOL_NAMES = {"schema.inspect", "query.read", "db.health", "query.write"}


async def _server(conn_manager: ConnectionManager, config: PgopsConfig) -> FastMCP:
    return build_server(config, conn_manager)


async def test_registered_tool_names_match_docs(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """Tool names are a public contract (docs/API.md, and whatever an agent was told
    to call). They must not drift with the Python function names behind them."""
    server = await _server(conn_manager, config)
    names = {t.name for t in await server.list_tools()}
    assert DOCUMENTED_TOOL_NAMES <= names, names


async def test_every_tool_result_is_json_encodable(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = await _server(conn_manager, config)
    calls = [
        ("schema.inspect", {"level": "full"}),
        ("schema.inspect", {"level": "tables"}),
        ("schema.inspect", {"level": "summary"}),
        ("query.read", {"sql": "SELECT * FROM items ORDER BY id", "limit": 5}),
        ("db.health", {}),
    ]
    for name, args in calls:
        result = await server.call_tool(name, args)
        assert result.is_error is False, (name, result)
        json.dumps(result.structured_content)


async def test_refusal_returns_structured_error_not_exception(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = await _server(conn_manager, config)
    result = await server.call_tool("query.read", {"sql": "DROP TABLE items"})
    # a refusal is a normal, parseable result — not a transport-level error the agent
    # has to interpret from a stack trace
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["error"]["code"] == "CLASSIFICATION_REFUSED"


async def test_postgres_error_surfaces_as_structured_error(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    server = await _server(conn_manager, config)
    result = await server.call_tool("query.read", {"sql": "SELECT * FROM no_such_table"})
    assert result.structured_content is not None
    assert result.structured_content["error"]["code"] == "INVALID_ARGUMENT"


async def test_write_tool_absent_in_read_only_mode(dsn: str) -> None:
    """--read-only removes the tool from the advertised surface entirely, rather than
    registering it and refusing at call time. An agent cannot be tempted by a tool it
    was never told exists."""
    ro_config = PgopsConfig.from_env(dsn=dsn, read_only=True)
    manager = ConnectionManager(ro_config)
    await manager.start()
    try:
        server = build_server(ro_config, manager)
        names = {t.name for t in await server.list_tools()}
        assert "query.write" not in names
        assert "query.read" in names
    finally:
        await manager.stop()


async def test_confirmation_flow_end_to_end_through_mcp(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """The whole Phase 2 contract as an agent experiences it: refused with a reason and
    a token, then executed when called again with that token."""
    server = await _server(conn_manager, config)

    refusal = await server.call_tool("query.write", {"sql": "DELETE FROM items"})
    assert refusal.structured_content is not None
    error = refusal.structured_content["error"]
    assert error["code"] == "CONFIRMATION_REQUIRED"
    assert "every row" in error["message"]

    token = error["hint"].split("confirm_token=")[1].split("'")[1]
    executed = await server.call_tool(
        "query.write", {"sql": "DELETE FROM items", "confirm_token": token}
    )
    assert executed.structured_content is not None
    assert executed.structured_content["rows_affected"] == 250
    json.dumps(executed.structured_content)


async def test_migration_plan_publishes_the_target_grammar(
    conn_manager: ConnectionManager, config: PgopsConfig
) -> None:
    """`target` used to reach clients as `{"type": "object", "additionalProperties": true}`.

    An MCP client feeds the input schema to the model, so the flagship tool of this
    server was saying "any object will do" in the one place the model is guaranteed to
    read. The grammar lived in prose in the description, and observed first guesses got
    it wrong in both of the ways prose invites: `{"primary_key": true}` on a column,
    and `{"columns": [...]}` as an index definition.

    This also guards the startup failure that adding the schema caused. `TargetSchema`
    was first defined inside build_server, and because `from __future__ import
    annotations` turns every annotation into a string that pydantic resolves against
    module globals, registration raised `NameError: name 'TargetSchema' is not defined`
    — taking the whole server down at boot. Any test that merely imports the module
    misses it; this one builds the server.
    """
    server = await _server(conn_manager, config)
    tool = await server.get_tool("migration.plan")
    schema = tool.parameters["properties"]["target"]
    tables = schema["properties"]["tables"]["additionalProperties"]
    assert set(tables["properties"]) == {"columns", "constraints", "indexes"}
    column = tables["properties"]["columns"]["additionalProperties"]
    assert column["required"] == ["type"]
    assert set(column["properties"]) == {"type", "nullable", "default"}
    # A primary key is a constraint here, and the schema must not imply otherwise.
    assert "primary_key" not in column["properties"]
    assert column["additionalProperties"] is False
    # An index is a column list — a string or a list of strings, never an object.
    index = tables["properties"]["indexes"]["additionalProperties"]
    assert {"type": "string"} in index["anyOf"]


async def test_read_only_mode_also_withholds_the_container_state_changers(dsn: str) -> None:
    """PGOPS_READ_ONLY=1 must mean this server changes nothing.

    `container.restart` and `container.exec` were gated on approval mode alone, so a
    read-only server started with approval mode on still advertised both. Restarting the
    database container drops every open connection and loses in-flight transactions,
    which is not a read by any reading of the word — and container.exec runs commands on
    the host of the database. Probed on a live server: with PGOPS_READ_ONLY=1 the write
    tools were correctly absent and these two were still listed.
    """
    ro_config = PgopsConfig.from_env(dsn=dsn, read_only=True, approval_mode=True)
    manager = ConnectionManager(ro_config)
    await manager.start()
    try:
        names = {t.name for t in await build_server(ro_config, manager).list_tools()}
        assert "container.restart" not in names
        assert "container.exec" not in names
        # Read-only diagnostics stay: they are the point of the mode.
        assert "container.logs" in names
        assert "env.topology" in names
    finally:
        await manager.stop()
