"""Release notes are generated from CHANGELOG.md, not written twice.

Two hand-written descriptions of one release drift immediately, and the one nobody
regenerates is the one that goes stale. Generating means the changelog is the single
source of truth — but it also means a missing changelog section breaks a release at the
worst moment, so that failure is checked here instead.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from release_notes import build, extract


def current_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return str(project["version"])


def test_the_version_being_shipped_has_a_changelog_section() -> None:
    """The check that matters. Without it, `gh release create` fails after PyPI has
    already accepted an immutable version number."""
    notes = build(current_version())
    assert notes.strip()


def test_a_missing_section_fails_loudly_with_advice() -> None:
    with pytest.raises(SystemExit) as exc_info:
        extract("# Changelog\n\n## [9.9.9]\n- something\n", "1.2.3")
    assert "CHANGELOG" in str(exc_info.value)


def test_only_the_requested_version_is_extracted() -> None:
    changelog = (
        "# Changelog\n\n"
        "## [2.0.0] — 2026-01-01\n### Added\n- new thing\n\n"
        "## [1.0.0] — 2025-01-01\n### Added\n- old thing\n"
    )
    body = extract(changelog, "1.0.0")
    assert "old thing" in body
    assert "new thing" not in body


def test_link_reference_definitions_are_trimmed() -> None:
    """Markdown link refs at the bottom of a changelog are not release content; rendered
    on a release page they are noise."""
    changelog = "## [1.0.0]\n- a change\n\n[1.0.0]: https://example.com/tag/v1.0.0\n"
    body = extract(changelog, "1.0.0")
    assert "a change" in body
    assert "example.com" not in body


def test_notes_lead_with_what_it_is_and_how_to_install() -> None:
    """A release page is often someone's first look at the project. The changelog alone
    assumes they already know what it is."""
    notes = build(current_version())
    head = notes[:900]
    assert "PostgreSQL" in head
    assert "uvx" in head and "pgops-mcp" in head
    assert "mcpServers" in head, "the client config block is the thing people copy"


def test_notes_link_the_docs() -> None:
    notes = build(current_version())
    for page in ("GETTING_STARTED.md", "API.md", "SETUP.md", "SECURITY.md"):
        assert page in notes


def test_notes_are_encodable_as_utf8() -> None:
    """The generator writes bytes rather than text because `sys.stdout.write` encodes
    with the console codepage — cp1252 on Windows — which mangles every em-dash and
    produces a release body GitHub renders as mojibake."""
    notes = build(current_version())
    assert notes.encode("utf-8").decode("utf-8") == notes
    assert "�" not in notes
