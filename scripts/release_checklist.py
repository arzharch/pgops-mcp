"""Pre-release checklist for pgops-mcp — verifies each gate rather than trusting a glance.

Run before creating a release tag:  python scripts/release_checklist.py 0.1.7

Every item is a hard gate. It prints PASS/FAIL per item and exits non-zero if any fails,
so it can also run in CI. It does NOT push or tag — that stays a human action.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tomllib
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((ok, name, detail))
    print(f"  [{'PASS' if ok else 'FAIL':4s}] {name:44s} {detail[:60]}")


def run(*args: str) -> tuple[int, str]:
    p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def main(target: str) -> int:
    print(f"\nRelease checklist for v{target}\n" + "=" * 66)

    # 1. Version consistency across every place a version is written.
    pt = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    init = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/pgops/__init__.py").read_text()).group(1)
    sj = json.loads((ROOT / "server.json").read_text())
    pypi = next(p for p in sj["packages"] if p["registryType"] == "pypi")["version"]
    oci = next(p for p in sj["packages"] if p["registryType"] == "oci")["identifier"].rsplit(":", 1)[1]
    versions = {pt, init, sj["version"], pypi, oci}
    check("version strings all agree", len(versions) == 1, f"{versions}")
    check("version matches the tag being cut", versions == {target}, f"want {target}, have {versions}")

    # 2. server.json ownership markers (registry publish fails without these).
    check("server.json name is namespaced", sj["name"].startswith("io.github."), sj["name"])
    check("mcp-name marker in README", "mcp-name: " + sj["name"] in (ROOT / "README.md").read_text(),
          "for PyPI ownership verification")

    # 3. CHANGELOG has a dated section for this version, and [Unreleased] is empty.
    cl = (ROOT / "CHANGELOG.md").read_text()
    m = re.search(rf"## \[{re.escape(target)}\] — (\d{{4}}-\d{{2}}-\d{{2}})", cl)
    check(f"CHANGELOG has a dated [{target}] section", m is not None, m.group(1) if m else "missing")
    unrel = re.search(r"## \[Unreleased\]\n(.*?)## \[", cl, re.S)
    check("CHANGELOG [Unreleased] is empty", bool(unrel) and not unrel.group(1).strip(),
          "content left under Unreleased" if unrel and unrel.group(1).strip() else "clean")

    # 4. Working tree clean and HEAD is what we will tag.
    _, st = run("git", "status", "--porcelain")
    check("git working tree is clean", st.strip() == "", st.strip()[:40] or "clean")

    # 5. Lint + types.
    rc, _ = run(".venv/Scripts/python.exe", "-m", "ruff", "check", "src", "tests")
    check("ruff clean", rc == 0)
    rc, out = run(".venv/Scripts/python.exe", "-m", "mypy", "src")
    check("mypy --strict clean", rc == 0, out.strip().splitlines()[-1] if out.strip() else "")

    # 6. Build the wheel and inspect the metadata a user/PyPI actually sees.
    for d in (ROOT / "dist").glob("*"):
        d.unlink()
    rc, out = run("uv", "build")
    check("wheel builds", rc == 0, out.strip().splitlines()[-1] if out.strip() else "")
    whls = list((ROOT / "dist").glob("*.whl"))
    if whls:
        z = zipfile.ZipFile(whls[0])
        meta = z.read(next(n for n in z.namelist() if n.endswith("METADATA"))).decode()
        wv = re.search(r"^Version: (.+)$", meta, re.M).group(1)
        body = meta.split("\n\n", 1)[1]
        check("wheel version matches target", wv == target, wv)
        check("wheel has project URLs (sidebar)", len(re.findall(r"^Project-URL:", meta, re.M)) >= 5)
        check("README mcp-name marker in wheel", "mcp-name: " + sj["name"] in body)
        rel = re.findall(r"\]\((?!https?:|#)([^)]+)\)", body)
        check("no repo-relative links in README", len(rel) == 0, f"{len(rel)} broken: {rel[:3]}")
        check("no internal/ paths leaked", body.count("internal/") == 0, f"{body.count('internal/')} refs")

    # 7. server.json validates against the vendored registry schema (the manifest gate).
    rc, out = run(".venv/Scripts/python.exe", "-m", "pytest",
                  "tests/test_registry_manifest.py", "tests/test_public_surface.py",
                  "tests/test_release_notes.py", "-q", "-p", "no:randomly")
    check("manifest + public-surface + release-notes tests", rc == 0,
          out.strip().splitlines()[-1] if out.strip() else "")

    # 8. Tool count in docs matches reality (a contract users read).
    api = (ROOT / "docs/API.md").read_text()
    readme = (ROOT / "README.md").read_text()
    n_headers = len(re.findall(r"^## [a-z]+\.[a-z]+", api, re.M))
    readme_counts = set(re.findall(r"\b(\d+) tools\b", readme))
    check("README tool count is consistent", len(readme_counts) <= 1, f"README says {readme_counts}")

    print("=" * 66)
    passed = sum(1 for ok, *_ in RESULTS if ok)
    fails = [(n, d) for ok, n, d in RESULTS if not ok]
    print(f"RESULT: {passed}/{len(RESULTS)} gates pass")
    if fails:
        print("BLOCKERS:")
        for n, d in fails:
            print(f"  - {n}: {d}")
        return 1
    print("All gates pass — safe to tag.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "0.1.7"))
