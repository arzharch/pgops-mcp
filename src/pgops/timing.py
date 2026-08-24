"""Elapsed-time measurement for durations reported to callers and to the audit log.

Why this exists rather than `time.monotonic()` inline at each call site:

`time.monotonic()` on Windows has a resolution of **15.625 ms** — it is backed by the
system tick counter. Measured on the development machine:

    monotonic    resolution 0.015625 s -> measured a 10 ms sleep as   0.000 ms
    perf_counter resolution 1e-07    s -> measured a 10 ms sleep as  10.470 ms

Every duration this project reports is shorter than that tick for a healthy operation,
so `monotonic` reported `0.0` for real work. It was caught in the migration ledger,
which recorded `duration_ms = 0` for a migration whose own `started_at`/`finished_at`
timestamps were 9.8 ms apart — the row disagreed with itself.

That matters beyond cosmetics: `duration_ms` in the audit log is forensic data. "How
long did that DELETE hold its locks?" is a question an incident review asks, and an
answer of `0.0` is not just imprecise, it is wrong in a way that looks like the
instrumentation is broken.

`perf_counter` is the documented API for measuring short intervals, is monotonic, and
uses the high-resolution performance counter on every platform. `monotonic` remains the
right choice for long deadlines (see the confirmation-token TTL in guardrails.py), where
tick resolution is irrelevant and the semantics are "has this expired".
"""

from __future__ import annotations

import time


class Elapsed:
    """Context-free stopwatch. `start()` at the beginning, `.ms` whenever needed."""

    __slots__ = ("_start",)

    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def ms(self) -> float:
        return (time.perf_counter() - self._start) * 1000

    def rounded(self, digits: int = 2) -> float:
        return round(self.ms, digits)
