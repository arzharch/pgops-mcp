"""Config resolution: caps, validation, env parsing. No database needed."""

from __future__ import annotations

import pytest

from pgops.config import PgopsConfig, RowLimits, TimeoutTiers
from pgops.errors import ErrorCode, PgopsError


def test_timeout_defaults_and_ceiling() -> None:
    tiers = TimeoutTiers(default_ms=5000, max_ms=30000)
    assert tiers.resolve(None) == 5000
    assert tiers.resolve(1000) == 1000
    # a caller asking for more than the server ceiling is silently clamped, not refused:
    # the request is still serviceable, just not on the caller's terms
    assert tiers.resolve(999_999) == 30000


def test_timeout_rejects_nonpositive() -> None:
    tiers = TimeoutTiers(default_ms=5000, max_ms=30000)
    with pytest.raises(PgopsError) as exc_info:
        tiers.resolve(0)
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT


def test_row_limit_defaults() -> None:
    limits = RowLimits(default=100, max=10_000)
    assert limits.resolve(None) == 100
    assert limits.resolve(50) == 50


def test_row_limit_over_max_is_refused_not_clamped() -> None:
    """Deliberately different from timeouts: silently returning fewer rows than asked
    for would make an agent believe it had seen the whole result set. Wrong data is
    worse than a clear refusal, so this one errors."""
    limits = RowLimits(default=100, max=10_000)
    with pytest.raises(PgopsError) as exc_info:
        limits.resolve(10_001)
    assert exc_info.value.code is ErrorCode.ROW_LIMIT_EXCEEDED


def test_missing_dsn_is_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PGOPS_DSN", raising=False)
    with pytest.raises(PgopsError) as exc_info:
        PgopsConfig.from_env()
    assert exc_info.value.code is ErrorCode.DSN_MISSING
    assert exc_info.value.hint is not None


def test_read_only_flag_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGOPS_DSN", "postgresql://localhost/x")
    monkeypatch.setenv("PGOPS_READ_ONLY", "true")
    assert PgopsConfig.from_env().read_only is True


def test_cli_flag_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGOPS_DSN", "postgresql://localhost/from-env")
    config = PgopsConfig.from_env(dsn="postgresql://localhost/from-cli")
    assert "from-cli" in config.dsn


def test_invalid_int_env_is_structured_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGOPS_DEFAULT_ROW_LIMIT", "not-a-number")
    with pytest.raises(PgopsError) as exc_info:
        RowLimits()
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT
