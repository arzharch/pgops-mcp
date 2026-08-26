"""Build GitHub Release notes for a version from CHANGELOG.md.

One source of truth. Hand-writing the release body separately means it drifts from the
changelog immediately — they say the same thing, and the one nobody regenerates is the
one that goes stale.

The changelog is written for people who already use the project. A release page is often
someone's *first* look at it, so this wraps the extracted section in the two things a
newcomer actually needs first: what the thing is, and how to install it.

Usage:
    python scripts/release_notes.py 0.1.1 > notes.md
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INTRO = """\
**pgops-mcp lets an AI assistant operate a PostgreSQL database safely.**

Point it at a database and your assistant can inspect the schema, run queries, diagnose
slow ones, and plan migrations — without being able to quietly destroy anything. Every
statement is classified before it runs, anything destructive stops and asks a human, and
everything that happens is written to an append-only audit log.

## Install

Add to Claude Desktop, Cursor, or VS Code:

```json
{
  "mcpServers": {
    "pgops": {
      "command": "uvx",
      "args": ["pgops-mcp"],
      "env": { "PGOPS_DSN": "postgresql://user:pass@localhost:5432/mydb" }
    }
  }
}
```

Prefer a container? Swap `uvx` for `docker run -i --rm ghcr.io/arzharch/pgops-mcp` —
see the [README](https://github.com/arzharch/pgops-mcp#install).

Check the connection before wiring a client to it:

```bash
uvx pgops-mcp --selfcheck --dsn "postgresql://user:pass@localhost:5432/mydb"
```

---
"""

OUTRO = """
---

**Docs:** [Getting started](https://github.com/arzharch/pgops-mcp/blob/main/docs/GETTING_STARTED.md)
· [Tool reference](https://github.com/arzharch/pgops-mcp/blob/main/docs/API.md)
· [Setup & configuration](https://github.com/arzharch/pgops-mcp/blob/main/SETUP.md)
· [Security model](https://github.com/arzharch/pgops-mcp/blob/main/SECURITY.md)
"""


def extract(changelog: str, version: str) -> str:
    """Return the body of the `## [version]` section, without its heading.

    Matches the heading, then everything up to the next `## ` at column zero. A link
    reference block at the bottom of the file is not a section, so it is trimmed.
    """
    pattern = rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)"
    match = re.search(pattern, changelog, re.MULTILINE | re.DOTALL)
    if match is None:
        raise SystemExit(f"no '## [{version}]' section in CHANGELOG.md — add one before tagging")
    body = match.group(1)
    # Drop trailing link-reference definitions (`[0.1.1]: https://...`).
    body = re.sub(r"^\[[^\]]+\]:\s*http\S+\s*$", "", body, flags=re.MULTILINE)
    return body.strip()


def build(version: str) -> str:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    return f"{INTRO}\n{extract(changelog, version)}\n{OUTRO}"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: release_notes.py <version>")
    version = sys.argv[1].lstrip("v")

    # Write bytes, not text. `sys.stdout.write` encodes with the console's codepage,
    # which on a Windows terminal is cp1252 — redirecting the output there silently
    # mangles every em-dash to 0x97 and produces a file that is not valid UTF-8. That is
    # the same failure that left mojibake through six documents in this repo, and the
    # release body is exactly the wrong place for it: GitHub renders it verbatim.
    sys.stdout.buffer.write(build(version).encode("utf-8"))


if __name__ == "__main__":
    main()
