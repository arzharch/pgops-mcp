"""Docker environment awareness (Phase 5, PRD FR-5).

Two things dominate the design of this module, and neither is about Docker features.

**1. The Docker socket is root-equivalent.** Anything able to talk to it can mount the
host filesystem into a privileged container and own the machine. So the default posture
is read-only: list, inspect, logs, stats. `container.restart` and `container.exec` are
gated *twice* — the server must have been started with `--approval-mode`, AND the call
needs a confirmation token. Neither gate alone is enough: the flag is the operator
saying "this deployment may act", the token is a human approving this specific action.

**2. Container metadata is full of secrets.** `container.attrs['Config']['Env']` on the
dev database contains, verbatim:

    POSTGRES_PASSWORD=pgops_dev

Environment variables are where credentials live — database passwords, API keys, signing
secrets. A topology tool that returns raw container attributes hands all of it to the
agent, into its context window, and onward into whatever logs that context reaches. So
this module builds an explicit allowlist of fields to return rather than filtering a
denylist out of the raw attrs: with a denylist, the next Docker API version that adds a
secret-bearing field leaks it by default. There is a test asserting the known password
never appears anywhere in the output.

Blocking I/O: the Docker SDK is synchronous, and `stats(stream=False)` takes ~1 second
because it samples twice to compute a CPU delta. Calling that directly from an async
tool would freeze the event loop for every other request. Every SDK call therefore goes
through `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from pgops.audit import AuditEntry, AuditLog
from pgops.config import PgopsConfig
from pgops.errors import ErrorCode, PgopsError
from pgops.guardrails import ConfirmationTokenStore

logger = logging.getLogger("pgops.environment")

# Postgres severities, weakest to strongest. Used for log filtering.
_SEVERITY_ORDER = ["DEBUG", "INFO", "NOTICE", "LOG", "WARNING", "ERROR", "FATAL", "PANIC"]
_SEVERITY_RE = re.compile(
    r"\b(DEBUG[1-5]?|INFO|NOTICE|LOG|WARNING|ERROR|FATAL|PANIC)\b:", re.IGNORECASE
)

# `container.exec` is arbitrary code execution inside a container. Even behind two gates,
# handing an agent an unrestricted shell is a different class of capability from
# "restart this container". The default is therefore an allowlist of read-only
# diagnostic commands; anything else is refused with an explanation. An operator who
# genuinely wants a shell has one — this tool is not the right way to get it.
#
# `psql` and `postgres` were on this list and are not any more, because both defeat the
# rule the list exists to enforce. Verified against a live container:
#
#     psql -U u -d d -c "CREATE TABLE pwned(x int)"   -> exit 0, table created
#     psql -U u -d d -c "\! id"                       -> uid=0(root) gid=0(root)
#
# The first bypasses every SQL safety layer in this server — classification, scope
# enforcement, the migration ledger, the lock analysis — because none of it sees SQL
# that travels as an argv string to a client binary. The second is `\!`, psql's shell
# escape, which is precisely the arbitrary shell the paragraph above says this tool
# does not offer. `pg_isready` covers the diagnostic case (is the server accepting
# connections) without carrying a SQL interpreter, and anything needing actual SQL has
# query.read, which is audited and classified.
_EXEC_ALLOWLIST = {
    "ps",
    "df",
    "free",
    "uptime",
    "cat",
    "ls",
    "env",
    "netstat",
    "ss",
    "pg_isready",
    "id",
    "whoami",
    "hostname",
    "date",
}


def _docker_module() -> Any:
    try:
        import docker
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise PgopsError(
            ErrorCode.DOCKER_UNAVAILABLE,
            "the docker package is not installed",
            hint="install pgops-mcp with the docker extra",
        ) from exc
    return docker


async def _client() -> Any:
    """Connect to the Docker daemon, or fail with a structured, actionable error.

    Docker being unavailable must degrade this tool group only — the database tools are
    unaffected and must keep working (ARCHITECTURE.md failure modes).
    """
    docker = _docker_module()

    def _connect() -> Any:
        client = docker.from_env()
        client.ping()
        return client

    try:
        return await asyncio.to_thread(_connect)
    except Exception as exc:
        raise PgopsError(
            ErrorCode.DOCKER_UNAVAILABLE,
            f"cannot reach the Docker daemon: {exc}",
            hint="is Docker running, and does this user have access to the socket?",
        ) from exc


def _dsn_port(dsn: str) -> int | None:
    try:
        parsed = urlparse(dsn)
        return parsed.port
    except ValueError:
        return None


def _published_ports(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    ports: list[dict[str, Any]] = []
    raw = (attrs.get("NetworkSettings") or {}).get("Ports") or {}
    for container_port, bindings in raw.items():
        for binding in bindings or []:
            ports.append(
                {
                    "container_port": container_port,
                    "host_ip": binding.get("HostIp"),
                    "host_port": int(binding["HostPort"]) if binding.get("HostPort") else None,
                }
            )
    return ports


@dataclass(slots=True)
class ContainerInfo:
    """Deliberately an allowlist of safe fields — see the module docstring on secrets."""

    name: str
    id_short: str
    image: str
    status: str
    health: str | None
    compose_project: str | None
    compose_service: str | None
    ports: list[dict[str, Any]] = field(default_factory=list)
    mounts: list[str] = field(default_factory=list)
    serves_our_dsn: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "id": self.id_short,
            "image": self.image,
            "status": self.status,
            "health": self.health,
            "compose_project": self.compose_project,
            "compose_service": self.compose_service,
            "ports": self.ports,
            "mounts": self.mounts,
            "serves_our_dsn": self.serves_our_dsn,
        }


def _container_info(container: Any, dsn_port: int | None) -> ContainerInfo:
    attrs = container.attrs
    labels = container.labels or {}
    ports = _published_ports(attrs)
    # Match the container serving our DSN by *published host port*, not by image name.
    # This machine runs several postgres containers at once (5435 is ours, 5432/5433/5434
    # belong to other projects); matching on "the image is postgres" would confidently
    # pick the wrong one and then report another project's logs as our database's.
    serves = dsn_port is not None and any(p["host_port"] == dsn_port for p in ports)
    return ContainerInfo(
        name=container.name,
        id_short=container.short_id,
        image=(container.image.tags or ["<untagged>"])[0],
        status=container.status,
        health=(attrs.get("State") or {}).get("Health", {}).get("Status"),
        compose_project=labels.get("com.docker.compose.project"),
        compose_service=labels.get("com.docker.compose.service"),
        ports=ports,
        # mount *destinations* only: a source path leaks host filesystem layout
        mounts=[m.get("Destination", "") for m in (attrs.get("Mounts") or [])],
        serves_our_dsn=serves,
    )


async def env_topology(config: PgopsConfig, all_containers: bool = False) -> dict[str, Any]:
    client = await _client()
    dsn_port = _dsn_port(config.dsn)

    def _list() -> list[Any]:
        containers: list[Any] = client.containers.list(all=all_containers)
        return containers

    containers = await asyncio.to_thread(_list)
    infos = [_container_info(c, dsn_port) for c in containers]

    projects: dict[str, list[str]] = {}
    for info in infos:
        projects.setdefault(info.compose_project or "<no compose project>", []).append(info.name)

    ours = [i for i in infos if i.serves_our_dsn]
    return {
        "dsn_host_port": dsn_port,
        "database_container": ours[0].to_dict() if ours else None,
        "database_container_note": (
            None
            if ours
            else (
                f"no running container publishes host port {dsn_port} — the database may "
                "be running outside Docker, on another host, or on a port not published "
                "to the host"
            )
        ),
        "containers": [i.to_dict() for i in infos],
        "compose_projects": projects,
    }


def _line_severity(line: str) -> str | None:
    match = _SEVERITY_RE.search(line)
    if not match:
        return None
    severity = match.group(1).upper()
    return "DEBUG" if severity.startswith("DEBUG") else severity


async def container_logs(
    name: str, tail: int = 100, min_severity: str | None = None, since_seconds: int | None = None
) -> dict[str, Any]:
    client = await _client()

    def _fetch() -> str:
        container = client.containers.get(name)
        kwargs: dict[str, Any] = {"tail": max(1, min(tail, 2000))}
        if since_seconds:
            import time as _time

            kwargs["since"] = int(_time.time()) - since_seconds
        text: str = container.logs(**kwargs).decode("utf-8", errors="replace")
        return text

    try:
        raw = await asyncio.to_thread(_fetch)
    except Exception as exc:
        raise _not_found_or_error(name, exc) from exc

    lines = [ln for ln in raw.splitlines() if ln.strip()]
    filtered = lines
    if min_severity:
        wanted = min_severity.upper()
        if wanted not in _SEVERITY_ORDER:
            raise PgopsError(
                ErrorCode.INVALID_ARGUMENT,
                f"unknown severity {min_severity!r}",
                hint=f"one of {_SEVERITY_ORDER}",
            )
        threshold = _SEVERITY_ORDER.index(wanted)
        # Lines with no recognizable severity are dropped when filtering: a continuation
        # line of a multi-line statement carries no level of its own, and guessing would
        # either flood the result or hide the context of a real error.
        filtered = [
            ln
            for ln in lines
            if (sev := _line_severity(ln)) is not None and _SEVERITY_ORDER.index(sev) >= threshold
        ]

    return {
        "container": name,
        "lines": filtered,
        "returned": len(filtered),
        "scanned": len(lines),
        "min_severity": min_severity,
    }


def _cpu_percent(stats: dict[str, Any]) -> float | None:
    cpu = stats.get("cpu_stats") or {}
    pre = stats.get("precpu_stats") or {}
    try:
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        system_delta = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
    except (KeyError, TypeError):
        return None
    if system_delta <= 0 or cpu_delta < 0:
        return None
    online = cpu.get("online_cpus") or 1
    percent: float = round((cpu_delta / system_delta) * online * 100, 2)
    return percent


def _memory(stats: dict[str, Any]) -> dict[str, Any]:
    mem = stats.get("memory_stats") or {}
    usage = mem.get("usage")
    limit = mem.get("limit")
    if usage is None or not limit:
        return {}
    # Match `docker stats`: subtract inactive page cache, which is reclaimable and not
    # really "used". Reporting the raw figure would overstate pressure and turn the
    # correlation hint below into a false alarm.
    inactive = (mem.get("stats") or {}).get("inactive_file") or 0
    used = max(usage - inactive, 0)
    return {
        "used_bytes": used,
        "limit_bytes": limit,
        "percent": round(used / limit * 100, 2),
    }


async def container_stats(name: str) -> dict[str, Any]:
    client = await _client()

    def _fetch() -> dict[str, Any]:
        # stream=False samples twice (~1s) so a CPU delta can be computed at all;
        # a single sample has no previous reading to compare against.
        result: dict[str, Any] = client.containers.get(name).stats(stream=False)
        return result

    try:
        stats = await asyncio.to_thread(_fetch)
    except Exception as exc:
        raise _not_found_or_error(name, exc) from exc

    blkio = stats.get("blkio_stats") or {}
    io_bytes = sum(
        entry.get("value", 0) for entry in (blkio.get("io_service_bytes_recursive") or [])
    )
    return {
        "container": name,
        "cpu_percent": _cpu_percent(stats),
        "memory": _memory(stats),
        "io_bytes_total": io_bytes,
        "throttling": (stats.get("cpu_stats") or {}).get("throttling_data"),
    }


def correlate(health_findings: list[dict[str, Any]], stats: dict[str, Any]) -> list[str]:
    """Join database symptoms with container resource pressure (PRD FR-5).

    Kept deliberately narrow. These are *hints* phrased as "consistent with", not
    diagnoses: correlation between a cache-hit dip and container memory pressure is
    suggestive, not proof, and a tool that states it as fact would send someone
    resizing a container when the real cause was a missing index.
    """
    hints: list[str] = []
    memory = stats.get("memory") or {}
    mem_pct = memory.get("percent")
    cpu_pct = stats.get("cpu_percent")
    categories = {f.get("category"): f for f in health_findings}

    if mem_pct is not None and mem_pct > 85:
        hint = f"container memory is at {mem_pct}% of its limit"
        cache = categories.get("cache_hit_ratio")
        if cache and cache.get("severity") in {"warning", "critical"}:
            hint += (
                " and the buffer cache hit ratio is degraded — consistent with Postgres "
                "having too little memory to hold the working set. Raising the container "
                "memory limit (and shared_buffers with it) is the thing to test first"
            )
        else:
            hint += " — headroom is low even though the database is not yet showing symptoms"
        hints.append(hint)

    if cpu_pct is not None and cpu_pct > 85:
        long_running = categories.get("long_running_queries")
        hint = f"container CPU is at {cpu_pct}%"
        if long_running:
            hint += (
                " while queries are running long — consistent with CPU starvation rather "
                "than lock contention. Check query.explain before adding CPU"
            )
        hints.append(hint)

    throttling = stats.get("throttling") or {}
    if throttling.get("throttled_periods"):
        hints.append(
            f"the container has been CPU-throttled {throttling['throttled_periods']} times — "
            "it is hitting its CPU quota, which slows every query regardless of how well "
            "they are written"
        )

    if not hints:
        hints.append("no container resource pressure that would explain database symptoms")
    return hints


async def env_correlate(
    config: PgopsConfig, health_findings: list[dict[str, Any]]
) -> dict[str, Any]:
    topology = await env_topology(config)
    db_container = topology.get("database_container")
    if not db_container:
        return {
            "correlated": False,
            "reason": topology.get("database_container_note"),
            "hints": [],
        }
    stats = await container_stats(db_container["name"])
    return {
        "correlated": True,
        "container": db_container["name"],
        "stats": stats,
        "hints": correlate(health_findings, stats),
    }


def _not_found_or_error(name: str, exc: Exception) -> PgopsError:
    if type(exc).__name__ == "NotFound":
        return PgopsError(
            ErrorCode.CONTAINER_NOT_FOUND,
            f"no container named {name!r}",
            hint="run env.topology to list container names",
        )
    return PgopsError(ErrorCode.DOCKER_UNAVAILABLE, f"docker call failed: {exc}")


def _require_approval_mode(config: PgopsConfig, action: str) -> None:
    if not config.approval_mode:
        raise PgopsError(
            ErrorCode.APPROVAL_MODE_REQUIRED,
            f"{action} is disabled: the server was not started with --approval-mode",
            hint=(
                "restart pgops-mcp with --approval-mode (or PGOPS_APPROVAL_MODE=true) to "
                "permit container mutations; it is off by default because Docker socket "
                "access is equivalent to root on the host"
            ),
        )


async def container_restart(
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
    name: str,
    confirm_token: str | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """Gated twice: server flag, then a confirmation token for this container."""
    _require_approval_mode(config, "container.restart")

    subject = f"container.restart:{name}"
    if confirm_token is None:
        reason = (
            f"restarting {name!r} drops every open connection to it. If this is the "
            "database container, in-flight transactions are lost and the application "
            "will see errors until it reconnects."
        )
        token = tokens.issue(subject, reason)
        audit.record(
            AuditEntry(
                tool="container.restart",
                sql=subject,
                verdict="refused_pending_confirmation",
                classification="container_mutation",
                detail=reason,
            )
        )
        raise PgopsError(
            ErrorCode.CONFIRMATION_REQUIRED,
            reason,
            hint=f"call again with confirm_token={token!r}",
        )

    try:
        tokens.redeem(confirm_token, subject)
    except PgopsError as exc:
        audit.record(
            AuditEntry(
                tool="container.restart",
                sql=subject,
                verdict="refused_bad_token",
                classification="container_mutation",
                error_code=exc.code.value,
                detail=exc.message,
            )
        )
        raise

    client = await _client()

    def _restart() -> None:
        client.containers.get(name).restart(timeout=timeout)

    try:
        await asyncio.to_thread(_restart)
    except Exception as exc:
        raise _not_found_or_error(name, exc) from exc

    audit.record(
        AuditEntry(
            tool="container.restart",
            sql=subject,
            verdict="executed",
            classification="container_mutation",
        )
    )
    return {"restarted": True, "container": name}


async def container_exec(
    config: PgopsConfig,
    audit: AuditLog,
    tokens: ConfirmationTokenStore,
    name: str,
    command: list[str],
    confirm_token: str | None = None,
) -> dict[str, Any]:
    """Gated three times: server flag, command allowlist, confirmation token."""
    _require_approval_mode(config, "container.exec")

    if not command:
        raise PgopsError(ErrorCode.INVALID_ARGUMENT, "command must be a non-empty list")

    binary = command[0].rsplit("/", 1)[-1]
    if binary not in _EXEC_ALLOWLIST:
        raise PgopsError(
            ErrorCode.EXEC_NOT_ALLOWED,
            f"{binary!r} is not in the diagnostic command allowlist",
            hint=(
                f"allowed: {sorted(_EXEC_ALLOWLIST)}. This tool deliberately does not "
                "offer an arbitrary shell — that is a different class of capability from "
                "container diagnostics, and an operator who needs one already has docker exec"
            ),
        )

    subject = f"container.exec:{name}:{' '.join(command)}"
    if confirm_token is None:
        reason = f"running {' '.join(command)!r} inside {name!r}"
        token = tokens.issue(subject, reason)
        audit.record(
            AuditEntry(
                tool="container.exec",
                sql=subject,
                verdict="refused_pending_confirmation",
                classification="container_mutation",
                detail=reason,
            )
        )
        raise PgopsError(
            ErrorCode.CONFIRMATION_REQUIRED,
            reason,
            hint=f"call again with confirm_token={token!r}",
        )

    try:
        tokens.redeem(confirm_token, subject)
    except PgopsError as exc:
        audit.record(
            AuditEntry(
                tool="container.exec",
                sql=subject,
                verdict="refused_bad_token",
                classification="container_mutation",
                error_code=exc.code.value,
                detail=exc.message,
            )
        )
        raise

    client = await _client()

    def _exec() -> tuple[int, str]:
        result = client.containers.get(name).exec_run(command, demux=False)
        return result.exit_code, result.output.decode("utf-8", errors="replace")

    try:
        exit_code, output = await asyncio.to_thread(_exec)
    except Exception as exc:
        raise _not_found_or_error(name, exc) from exc

    audit.record(
        AuditEntry(
            tool="container.exec",
            sql=subject,
            verdict="executed",
            classification="container_mutation",
            detail=f"exit_code={exit_code}",
        )
    )
    return {
        "container": name,
        "command": command,
        "exit_code": exit_code,
        "output": output[:20_000],
    }
