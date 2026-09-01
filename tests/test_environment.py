"""Docker environment layer against the real daemon (Phase 5 gate, PRD FR-5).

Skipped when Docker is unavailable — this tool group is the one part of the project
that legitimately cannot run without it, and CI on a socketless runner should skip
rather than fail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pgops.audit import AuditLog
from pgops.config import PgopsConfig
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore
from pgops.tools.environment import (
    container_exec,
    container_logs,
    container_restart,
    container_stats,
    correlate,
    env_topology,
)


def _docker_available() -> bool:
    # Blind except is deliberate here: this is a capability probe, and every possible
    # reason Docker is unreachable (not installed, socket missing, permission denied,
    # daemon down) means the same thing — skip. Enumerating failure modes would only
    # risk a new one crashing collection instead of skipping.
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="Docker daemon unavailable")

# The dev compose stack, brought up by `docker compose up -d`.
DEV_CONTAINER = "pgops_dev_postgres"
DEV_DSN = "postgresql://pgops:pgops_dev@localhost:5436/pgops_demo"
DEV_PASSWORD = "pgops_dev"


def _config(**kwargs: Any) -> PgopsConfig:
    return PgopsConfig(dsn=DEV_DSN, **kwargs)


def _dev_stack_running() -> bool:
    try:
        import docker

        return docker.from_env().containers.get(DEV_CONTAINER).status == "running"
    except Exception:  # noqa: BLE001 - capability probe; any failure means "skip"
        return False


needs_dev_stack = pytest.mark.skipif(
    not _dev_stack_running(), reason="dev compose stack not running"
)


# --- topology -----------------------------------------------------------------------


async def test_topology_lists_containers() -> None:
    topology = await env_topology(_config())
    assert isinstance(topology["containers"], list)
    assert "compose_projects" in topology


@needs_dev_stack
async def test_topology_identifies_our_database_by_port() -> None:
    """This machine runs several postgres containers at once — ours on host port 5436,
    other projects' on 5433/5434. Matching on "the image is postgres" would
    confidently pick the wrong one and then report another project's logs as our
    database's, so the match is on the published host port from the DSN."""
    topology = await env_topology(_config())
    assert topology["dsn_host_port"] == 5436
    db = topology["database_container"]
    assert db is not None
    assert db["name"] == DEV_CONTAINER
    assert any(p["host_port"] == 5436 for p in db["ports"])


@needs_dev_stack
async def test_topology_never_leaks_container_environment() -> None:
    """The single most important assertion in this module.

    `container.attrs['Config']['Env']` contains `POSTGRES_PASSWORD=pgops_dev` verbatim,
    along with whatever API keys other containers hold. A topology tool that returns raw
    attributes hands all of it to the agent and into its context window. The module
    returns an explicit allowlist of fields rather than filtering a denylist, so a
    future Docker API field cannot leak by default.
    """
    topology = await env_topology(_config(), all_containers=True)
    serialized = json.dumps(topology)

    # Assert on the leak's actual signature — the `KEY=value` assignment form — rather
    # than the bare secret. The dev password `pgops_dev` is a substring of the container
    # *name* `pgops_dev_postgres`, so a naive `password not in output` check fails on a
    # perfectly safe response. A test that cries wolf about its own fixture gets
    # weakened or deleted later, which is how the real guarantee gets lost.
    assert f"POSTGRES_PASSWORD={DEV_PASSWORD}" not in serialized
    assert "POSTGRES_PASSWORD" not in serialized
    assert "PGDATA" not in serialized  # any other env var would arrive the same way

    # Belt and braces: prove the daemon really is offering the secret, so this test
    # cannot quietly start passing because the field moved or the fixture changed.
    import docker

    raw_env = docker.from_env().containers.get(DEV_CONTAINER).attrs["Config"]["Env"]
    assert any(e.startswith("POSTGRES_PASSWORD=") for e in raw_env), (
        "expected the daemon to expose the password; if not, this test proves nothing"
    )


@needs_dev_stack
async def test_topology_groups_by_compose_project() -> None:
    topology = await env_topology(_config())
    assert DEV_CONTAINER in topology["compose_projects"].get("pgops-mcp", [])


async def test_topology_notes_when_no_container_serves_the_dsn() -> None:
    """A database on another host, or not published to the host, is a normal situation —
    it must be explained rather than silently returning null."""
    topology = await env_topology(PgopsConfig(dsn="postgresql://localhost:59999/nope"))
    assert topology["database_container"] is None
    assert "59999" in topology["database_container_note"]


# --- logs ---------------------------------------------------------------------------


@needs_dev_stack
async def test_logs_returns_lines() -> None:
    result = await container_logs(DEV_CONTAINER, tail=20)
    assert result["returned"] > 0
    assert result["container"] == DEV_CONTAINER


@needs_dev_stack
async def test_logs_severity_filter_narrows_results() -> None:
    everything = await container_logs(DEV_CONTAINER, tail=200)
    errors_only = await container_logs(DEV_CONTAINER, tail=200, min_severity="ERROR")
    assert errors_only["returned"] <= everything["returned"]
    assert errors_only["scanned"] == everything["scanned"]


@needs_dev_stack
async def test_logs_reject_unknown_severity() -> None:
    with pytest.raises(PgopsError) as exc_info:
        await container_logs(DEV_CONTAINER, min_severity="LOUD")
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT


async def test_logs_unknown_container_is_structured_error() -> None:
    with pytest.raises(PgopsError) as exc_info:
        await container_logs("no-such-container-xyz")
    assert exc_info.value.code is ErrorCode.CONTAINER_NOT_FOUND


# --- stats --------------------------------------------------------------------------


@needs_dev_stack
async def test_stats_returns_cpu_and_memory() -> None:
    stats = await container_stats(DEV_CONTAINER)
    assert stats["cpu_percent"] is not None
    memory = stats["memory"]
    assert memory["used_bytes"] > 0
    assert 0 <= memory["percent"] <= 100
    json.dumps(stats)


@needs_dev_stack
async def test_stats_memory_matches_docker_accounting() -> None:
    """`docker stats` subtracts inactive page cache, which is reclaimable and not really
    "used". Reporting the raw figure would overstate pressure and make the correlation
    hints fire falsely — and would disagree with what the user sees in their terminal."""
    import docker

    raw = docker.from_env().containers.get(DEV_CONTAINER).stats(stream=False)
    mem = raw["memory_stats"]
    inactive = (mem.get("stats") or {}).get("inactive_file", 0)
    expected = mem["usage"] - inactive

    stats = await container_stats(DEV_CONTAINER)
    # sampled a moment apart, so compare within a tolerance rather than exactly
    assert abs(stats["memory"]["used_bytes"] - expected) < expected * 0.5


# --- correlation --------------------------------------------------------------------


def test_correlation_links_memory_pressure_to_cache_misses() -> None:
    findings = [{"category": "cache_hit_ratio", "severity": "warning", "summary": "0.82"}]
    stats = {"memory": {"percent": 94.0}, "cpu_percent": 10.0, "throttling": {}}
    hints = correlate(findings, stats)
    assert any("94.0%" in h and "working set" in h for h in hints)


def test_correlation_reports_cpu_throttling() -> None:
    hints = correlate(
        [], {"memory": {"percent": 10}, "cpu_percent": 5, "throttling": {"throttled_periods": 42}}
    )
    assert any("throttled 42 times" in h for h in hints)


def test_correlation_is_quiet_when_nothing_is_wrong() -> None:
    """A hint on every call is noise. Silence has to mean something."""
    hints = correlate([], {"memory": {"percent": 12}, "cpu_percent": 3, "throttling": {}})
    assert hints == ["no container resource pressure that would explain database symptoms"]


def test_correlation_hedges_rather_than_diagnoses() -> None:
    """Correlation is not causation, and a tool that states it as fact sends someone
    resizing a container when the real cause was a missing index."""
    findings = [{"category": "cache_hit_ratio", "severity": "critical", "summary": "0.5"}]
    hints = correlate(findings, {"memory": {"percent": 99}, "cpu_percent": 1, "throttling": {}})
    assert any("consistent with" in h for h in hints)


# --- the two gates ------------------------------------------------------------------


async def test_restart_refused_without_approval_mode(tmp_path: Path) -> None:
    """Gate one: the operator's flag. Docker socket access is root-equivalent on the
    host, so mutation is off unless the deployment explicitly opted in."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(PgopsError) as exc_info:
        await container_restart(
            _config(approval_mode=False), audit, ConfirmationTokenStore(), DEV_CONTAINER
        )
    assert exc_info.value.code is ErrorCode.APPROVAL_MODE_REQUIRED


async def test_restart_with_approval_mode_still_requires_a_token(tmp_path: Path) -> None:
    """Gate two: a human approving this specific action. The flag alone is not enough."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(PgopsError) as exc_info:
        await container_restart(
            _config(approval_mode=True), audit, ConfirmationTokenStore(), DEV_CONTAINER
        )
    assert exc_info.value.code is ErrorCode.CONFIRMATION_REQUIRED
    assert "drops every open connection" in exc_info.value.message
    # the refusal is audited even though nothing happened
    assert AuditLog(tmp_path / "audit.jsonl").read_all()[0]["verdict"] == (
        "refused_pending_confirmation"
    )


async def test_restart_token_is_bound_to_one_container(tmp_path: Path) -> None:
    """Approval to restart a staging container must not restart production."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    tokens = ConfirmationTokenStore()
    token = tokens.issue("container.restart:some-other-container", "test")
    with pytest.raises(PgopsError) as exc_info:
        await container_restart(
            _config(approval_mode=True), audit, tokens, DEV_CONTAINER, confirm_token=token
        )
    assert exc_info.value.code is ErrorCode.CONFIRMATION_MISMATCH


async def test_exec_refused_without_approval_mode(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(PgopsError) as exc_info:
        await container_exec(
            _config(approval_mode=False), audit, ConfirmationTokenStore(), DEV_CONTAINER, ["ps"]
        )
    assert exc_info.value.code is ErrorCode.APPROVAL_MODE_REQUIRED


@pytest.mark.parametrize(
    "command",
    [
        ["bash"],
        ["sh", "-c", "rm -rf /"],
        ["/bin/bash"],
        ["curl"],
        # psql was allowlisted until a live probe showed both of these succeeding:
        # `-c` runs arbitrary SQL around every safety layer in this server, and `\!` is
        # psql's own shell escape, which returned uid=0(root) from the database
        # container. pg_isready covers the diagnostic case without a SQL interpreter.
        ["psql", "-c", "CREATE TABLE pwned (x int)"],
        ["psql", "-c", r"\! id"],
        ["postgres", "--single"],
        # `env` was allowlisted as a diagnostic and is the launcher pattern: a rogue
        # agent ran `env sh -c 'id'` against the live 0.1.3 server and got uid=0(root),
        # and bare `env` dumped POSTGRES_PASSWORD. argv[0] being allowlisted is not
        # enough — an interpreter anywhere in the command is refused, so every one of
        # these launcher forms is blocked even if argv[0] were allowlisted.
        ["env", "sh", "-c", "id"],
        ["env"],
        ["env", "id"],
        ["nice", "bash", "-c", "id"],
        ["xargs", "sh"],
        ["timeout", "5", "bash", "-c", "id"],
        ["cat", "/etc/passwd", ";", "sh"],
        ["ls", "-la", "&&", "bash"],
    ],
)
async def test_exec_allowlist_blocks_shells_and_downloads(
    tmp_path: Path, command: list[str]
) -> None:
    """Even behind two gates, an arbitrary shell is a different class of capability from
    container diagnostics. `/bin/bash` is checked by basename so a path cannot slip past,
    and an interpreter named anywhere in the command — not just argv[0] — is refused, so
    an allowlisted binary cannot be used to launch one."""
    audit = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(PgopsError) as exc_info:
        await container_exec(
            _config(approval_mode=True), audit, ConfirmationTokenStore(), DEV_CONTAINER, command
        )
    assert exc_info.value.code is ErrorCode.EXEC_NOT_ALLOWED


async def test_exec_allowlisted_command_still_requires_a_token(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    with pytest.raises(PgopsError) as exc_info:
        await container_exec(
            _config(approval_mode=True), audit, ConfirmationTokenStore(), DEV_CONTAINER, ["ps"]
        )
    assert exc_info.value.code is ErrorCode.CONFIRMATION_REQUIRED


@needs_dev_stack
async def test_exec_runs_an_allowlisted_command_with_a_token(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    tokens = ConfirmationTokenStore()
    config = _config(approval_mode=True)
    with pytest.raises(PgopsError) as exc_info:
        await container_exec(config, audit, tokens, DEV_CONTAINER, ["pg_isready"])
    token = (exc_info.value.hint or "").split("confirm_token=")[1].split("'")[1]

    result = await container_exec(
        config, audit, tokens, DEV_CONTAINER, ["pg_isready"], confirm_token=token
    )
    assert result["exit_code"] == 0
    assert "accepting connections" in result["output"]
    assert [e["verdict"] for e in AuditLog(tmp_path / "audit.jsonl").read_all()][-1] == "executed"
