"""Duration measurement (see src/pgops/timing.py for the full finding).

Regression guard: `time.monotonic()` has 15.625 ms resolution on Windows and reported
`0.0` for every operation faster than one tick. The migration ledger recorded
`duration_ms = 0` for a migration whose own start/finish timestamps were 9.8 ms apart.
`duration_ms` is forensic data in the audit log, so a wrong-looking zero is a real defect.
"""

from __future__ import annotations

import time

from pgops.timing import Elapsed


def test_clock_resolution_is_sub_millisecond() -> None:
    """The property the whole module depends on. If this fails, durations are unreliable
    on this platform and `Elapsed` needs a different clock — not a looser assertion."""
    assert time.get_clock_info("perf_counter").resolution < 0.001


def test_measures_short_durations_that_monotonic_would_report_as_zero() -> None:
    elapsed = Elapsed()
    time.sleep(0.01)  # 10 ms — shorter than one Windows monotonic tick (15.625 ms)
    assert 5 < elapsed.ms < 200


def test_elapsed_increases() -> None:
    elapsed = Elapsed()
    first = elapsed.ms
    time.sleep(0.005)
    assert elapsed.ms > first


def test_a_fast_operation_is_not_reported_as_zero() -> None:
    """Even a trivial amount of work must register as non-zero, which is exactly what
    monotonic failed to do."""
    elapsed = Elapsed()
    sum(range(100_000))
    assert elapsed.ms > 0.0


def test_rounded_helper() -> None:
    elapsed = Elapsed()
    time.sleep(0.002)
    value = elapsed.rounded(2)
    assert value == round(value, 2)
    assert value > 0
