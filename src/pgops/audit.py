"""Append-only JSONL audit log (FR-6).

Format choice — JSONL over a database table or a structured log service:
- A table in the same Postgres we're operating on is circular: the audit trail for
  "who dropped that table" must survive the database being broken, and must not be
  writable by the same statements it records. A local file is independent of the
  target's health.
- JSONL (one JSON object per line) is append-only by construction — no rewrite step, no
  partial-file corruption risk from an interrupted write, greppable with standard tools,
  and parseable line-by-line even if the process died mid-line (the bad last line is
  discarded, everything before it is intact).

Every executed statement is recorded, and so is every *refusal* — a blocked
`DELETE FROM orders` is exactly the event an incident review needs to see, and it is
the one a naive "log what we ran" design silently discards.

The SQL text is stored alongside its SHA-256 hash (TOOLS.md convention): the hash makes
identical statements groupable and searchable without string-matching over text that
may contain literal values.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("pgops.audit")


def sql_fingerprint(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class AuditEntry:
    tool: str
    sql: str
    verdict: str
    classification: str
    duration_ms: float | None = None
    rows_affected: int | None = None
    error_code: str | None = None
    detail: str | None = None
    audit_id: str = ""
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ts": self.ts,
            "audit_id": self.audit_id,
            "tool": self.tool,
            "verdict": self.verdict,
            "classification": self.classification,
            "sql": self.sql,
            "sql_sha256": sql_fingerprint(self.sql),
        }
        for key, value in (
            ("duration_ms", self.duration_ms),
            ("rows_affected", self.rows_affected),
            ("error_code", self.error_code),
            ("detail", self.detail),
        ):
            if value is not None:
                body[key] = value
        return body


class AuditLog:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._counter = 0

    @property
    def path(self) -> Path:
        return self._path

    def record(self, entry: AuditEntry) -> str:
        self._counter += 1
        # monotonic-ish id that is unique per process run and readable in a log tail
        entry.audit_id = f"{int(time.time())}-{self._counter:05d}"
        line = json.dumps(entry.to_dict(), separators=(",", ":"))
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # "a" + a single write() of one line: the open-append-close cycle keeps the
            # file consistent if the process is killed, and POSIX guarantees appends
            # under O_APPEND are atomic for writes this small, so concurrent servers
            # sharing a path interleave lines rather than corrupting them.
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # An audit write failure must never take down the tool call that succeeded,
            # but it must be loud: this is the one log line an operator needs to see.
            logger.error("AUDIT WRITE FAILED (%s): %s", exc, line)
        return entry.audit_id

    def read_all(self) -> list[dict[str, Any]]:
        """Used by tests and incident review. Tolerates a torn final line."""
        if not self._path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("skipping unparseable audit line")
        return entries
