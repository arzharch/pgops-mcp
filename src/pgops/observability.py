"""OpenTelemetry instrumentation: spans, metrics, and health state.

This module is what turns "has an audit log" into "is observable". The design
constraint that shapes everything here: **telemetry must never break the operation it
describes.** Every export call is wrapped; a down collector degrades to a no-op, never
to a failed tool call. The audit log remains the system of record — this is the
*operational* view (latency, error rates, saturation), complementary to the *forensic*
view (who did what, approved by whom).

Three signals, each with a deliberate choice:

**Traces.** One span per tool call, created in `tool_boundary` so every tool is covered
by construction rather than by remembering to instrument each one. Spans carry the
attributes an incident responder needs: tool name, classification, verdict
(executed/refused/failed), error code, duration. Refusals are spans too — a spike in
CONFIRMATION_REQUIRED is itself an operational signal (an agent trying something it
shouldn't).

**Metrics.** The four that matter for a service like this:
- `pgops.tool.calls` counter by tool + verdict — traffic and refusal rates
- `pgops.tool.duration` histogram by tool — latency distribution, p99 for SLOs
- `pgops.pool.acquisitions` + timeouts — pool saturation
- `pgops.db.up` gauge — last known reachability of Postgres

**Health.** A `/health` HTTP endpoint (liveness) and `/ready` (readiness: can we
actually reach Postgres right now). These exist so a process manager / load balancer /
compose healthcheck has something standard to poll instead of scraping logs.

Configuration (all optional, all defaulting to off):
    PGOPS_OTEL_ENDPOINT   OTLP exporter endpoint, e.g. http://localhost:4317
    PGOPS_METRICS_PORT    port for the Prometheus pull endpoint (/metrics)
    PGOPS_HEALTH_PORT     port for /health and /ready

Everything degrades: no endpoint configured → no-op providers, zero overhead beyond a
few attribute dict lookups.
"""

from __future__ import annotations

import logging
import os
import time
from types import TracebackType
from typing import Any, Self

logger = logging.getLogger("pgops.observability")

# --- Configuration -------------------------------------------------------------------


def otel_endpoint() -> str | None:
    return os.environ.get("PGOPS_OTEL_ENDPOINT") or None


def metrics_port() -> int | None:
    raw = os.environ.get("PGOPS_METRICS_PORT")
    return int(raw) if raw else None


def health_port() -> int | None:
    raw = os.environ.get("PGOPS_HEALTH_PORT")
    return int(raw) if raw else None


# --- Provider setup -------------------------------------------------------------------

_tracer: Any = None
_meter: Any = None
_counters: dict[str, Any] = {}
_histograms: dict[str, Any] = {}
_gauges: dict[str, Any] = {}


def init_observability() -> bool:
    """Initialize OTel providers if PGOPS_OTEL_ENDPOINT is set. Returns whether
    telemetry is live. Safe to call multiple times; safe when otel isn't installed."""
    global _tracer, _meter

    endpoint = otel_endpoint()
    if not endpoint:
        logger.info("PGOPS_OTEL_ENDPOINT not set; telemetry disabled")
        return False

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "PGOPS_OTEL_ENDPOINT is set but opentelemetry packages are not installed; "
            "install with: uv sync --extra otel"
        )
        return False

    resource = Resource.create(
        {"service.name": "pgops-mcp", "service.version": _package_version()}
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(tracer_provider)
    _tracer = trace.get_tracer("pgops")

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=endpoint), export_interval_millis=10_000
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)
    _meter = metrics.get_meter("pgops")

    _init_instruments()
    logger.info("telemetry exporting to %s", endpoint)
    return True


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("pgops-mcp")
    except Exception:  # noqa: BLE001 - version metadata missing is non-fatal
        return "unknown"


def _init_instruments() -> None:
    if _meter is None:
        return
    _counters["calls"] = _meter.create_counter(
        "pgops.tool.calls", description="Tool invocations by tool and verdict", unit="{call}"
    )
    _histograms["duration"] = _meter.create_histogram(
        "pgops.tool.duration", description="Tool execution duration", unit="ms"
    )
    _counters["pool_timeouts"] = _meter.create_counter(
        "pgops.pool.timeouts", description="Pool acquisition timeouts", unit="{timeout}"
    )
    _gauges["db_up"] = _meter.create_gauge(
        "pgops.db.up", description="1 if the last DB contact succeeded, 0 otherwise"
    )


# --- Span creation --------------------------------------------------------------------


class ToolSpan:
    """Context manager for one tool invocation's span.

    Usage inside tool_boundary:

        with ToolSpan("query.write") as span:
            ... run the tool ...
            span.set_verdict("executed", classification="write", rows=3)

    If OTel is not initialized, everything is a cheap no-op.
    """

    def __init__(self, tool_name: str) -> None:
        self._tool = tool_name
        self._start = time.perf_counter()
        self._span: Any = None
        self._verdict = "unknown"
        self._attrs: dict[str, str | int | float | bool] = {}

    def __enter__(self) -> Self:
        if _tracer is not None:
            self._span = _tracer.start_span(f"tool.{self._tool}")
            self._span.set_attribute("pgops.tool", self._tool)
        return self

    def set_verdict(self, verdict: str, **attrs: Any) -> None:
        self._verdict = verdict
        for key, value in attrs.items():
            if isinstance(value, (str, int, float, bool)):
                self._attrs[f"pgops.{key}"] = value

    def record_exception(self, exc: BaseException) -> None:
        if self._span is not None:
            self._span.record_exception(exc)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        duration_ms = (time.perf_counter() - self._start) * 1000

        # Metrics are recorded even without a span — they're the cheaper signal and
        # the one dashboards are built on.
        calls = _counters.get("calls")
        if calls is not None:
            calls.add(1, {"tool": self._tool, "verdict": self._verdict})
        hist = _histograms.get("duration")
        if hist is not None:
            hist.record(duration_ms, {"tool": self._tool})

        if self._span is not None:
            self._span.set_attribute("pgops.verdict", self._verdict)
            for key, value in self._attrs.items():
                self._span.set_attribute(key, value)
            if exc_type is not None and exc_val is not None:
                self._span.record_exception(exc_val)
                self._span.set_attribute("error.type", exc_type.__name__)
            self._span.end()


def record_pool_timeout() -> None:
    counter = _counters.get("pool_timeouts")
    if counter is not None:
        counter.add(1)


def set_db_up(up: bool) -> None:
    """Record last-known DB reachability. Called from healthcheck paths."""
    gauge = _gauges.get("db_up")
    if gauge is not None:
        gauge.set(1 if up else 0)


# --- Health endpoints ------------------------------------------------------------------


async def run_health_endpoints(
    readiness_check: Any,
) -> None:
    """Serve /health (liveness) and /ready (readiness) until cancelled.

    `readiness_check` is an async callable returning True when the server can do its
    job (i.e., Postgres is reachable). Runs only if PGOPS_HEALTH_PORT is set.

    Liveness vs readiness is the distinction operators actually need: liveness failing
    means restart the process; readiness failing means stop sending traffic but don't
    restart (the database being down is not this process's fault).
    """
    port = health_port()
    if not port:
        return

    try:
        from aiohttp import web
    except ImportError:
        logger.warning(
            "PGOPS_HEALTH_PORT is set but aiohttp is not installed; "
            "install with: uv sync --extra otel"
        )
        return

    async def liveness(_request: Any) -> Any:
        return web.json_response({"status": "alive"})

    async def readiness(_request: Any) -> Any:
        ok = await readiness_check()
        return web.json_response({"status": "ready" if ok else "not_ready"}, status=200 if ok else 503)

    app = web.Application()
    app.router.add_get("/health", liveness)
    app.router.add_get("/ready", readiness)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    logger.info("health endpoints on 127.0.0.1:%s (/health, /ready)", port)
