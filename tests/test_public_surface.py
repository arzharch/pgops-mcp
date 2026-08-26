"""What the public repository and the PyPI page actually show.

Two failures this guards against, both found by looking rather than assuming:

1. **Working notes were published.** `internal/` held a running progress log and a file
   literally named `interview_prep.md`. Design documents (architecture, ADRs) are worth
   showing; a personal status log is not, and a public repository is judged on what it
   chose to include.

2. **The README's links were repo-relative.** PyPI renders the README with no notion of
   the repository it came from, so `](docs/API.md)` resolves to nothing there. The
   project page is the first thing an installer sees and every link on it was dead.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Names that must never be tracked, wherever they sit in the tree.
PRIVATE_NAMES = {"flow.md", "interview_prep.md", "PRD.md", "SPEC.md"}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return out.stdout.splitlines()


def test_working_notes_are_not_tracked() -> None:
    leaked = [f for f in tracked_files() if Path(f).name in PRIVATE_NAMES]
    assert not leaked, f"working notes are published: {leaked}"


def test_no_internal_directory_is_tracked() -> None:
    assert not [f for f in tracked_files() if f.startswith("internal/")]


def test_private_notes_directory_is_ignored() -> None:
    """They stay on disk — they are useful — but git must never pick them up, including
    via a `git add -A` that someone runs without thinking.

    The paths checked are files *inside* `.private/`, not the directory itself, and that
    is load-bearing rather than stylistic. The ignore rule is written `.private/` with a
    trailing slash, which matches directories only — so `git check-ignore .private`
    answers "not ignored" whenever the directory does not exist on disk, because git
    cannot tell an absent path is a directory. It exists on a developer's machine and
    never in CI (it is ignored, so it is never checked out), which is exactly how this
    passed locally and failed on the runner.

    Checking `.private/flow.md` also tests the thing that actually matters: whether a
    file in there could be committed.
    """
    for name in sorted(PRIVATE_NAMES):
        result = subprocess.run(
            ["git", "check-ignore", "-q", f".private/{name}"],
            cwd=ROOT,
            capture_output=True,
            check=False,  # a non-zero exit is the assertion, not an error
        )
        assert result.returncode == 0, f".private/{name} is not gitignored"


def test_design_docs_are_still_public() -> None:
    """The correction is not "hide everything". Architecture and decision records are the
    part a reviewer should see; removing them would make the repository worse."""
    for path in ("docs/ARCHITECTURE.md", "docs/SYSTEM_DESIGN.md", "docs/adr/README.md"):
        assert (ROOT / path).exists(), f"{path} should remain public"
    assert len(list((ROOT / "docs" / "adr").glob("ADR-*.md"))) >= 6


def test_no_document_links_into_the_private_notes() -> None:
    """A link to a file that is no longer published is a broken link for everyone else."""
    dangling = []
    for md in list(ROOT.glob("*.md")) + list((ROOT / "docs").rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        for name in PRIVATE_NAMES:
            if name in text:
                dangling.append(f"{md.name} -> {name}")
    assert not dangling, dangling


# --- the PyPI project page ------------------------------------------------------------


def test_readme_has_no_repo_relative_links() -> None:
    """PyPI renders the README standalone. Anything not absolute is dead there."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    relative = [
        target
        for _text, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)", readme)
        if not target.startswith(("http", "#", "mailto"))
    ]
    assert not relative, f"these links break on PyPI: {relative}"


def test_project_urls_give_pypi_a_sidebar() -> None:
    """Without these the project page offers no route to the docs at all."""
    import tomllib

    urls = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["urls"]
    for expected in ("Homepage", "Documentation", "Changelog", "Source", "Issues"):
        assert expected in urls, f"PyPI sidebar is missing {expected}"
    assert all(u.startswith("https://") for u in urls.values())
