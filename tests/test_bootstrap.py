"""Placeholder test so `uv run pytest` passes at Phase 0 gate.

Phase 1 replaces this with real tests:
- tests/test_classifier.py      (table-driven classification cases)
- tests/test_guardrails.py      (proven against real Postgres via testcontainers)
- tests/test_explain.py         (seeded slow-query scenarios)
- tests/test_migrations.py      (diff/plan/apply/rollback)
"""


def test_bootstrap() -> None:
    assert True
