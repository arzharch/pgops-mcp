"""`server.json` must stay publishable.

The MCP Registry rejects a manifest that fails its schema, and it does so at the last
step of a release — after PyPI has already accepted an immutable version number. That is
the wrong moment to discover a 166-character description in a field capped at 100, which
is exactly what validating against the published schema caught here.

These tests are offline: the schema is vendored rather than fetched, so the suite does
not depend on network access and a registry outage cannot turn CI red. The trade-off is
that the vendored copy can drift; `test_schema_version_is_the_one_we_declare` fails
loudly if the manifest starts pointing at a schema this repo does not have.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "server.json"
SCHEMA = Path(__file__).parent / "data" / "server.schema.json"

SERVER_NAME = "io.github.arzharch/pgops-mcp"


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return data


def test_manifest_validates_against_the_published_schema(manifest: dict[str, Any]) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(
        jsonschema.Draft7Validator(schema).iter_errors(manifest),
        key=lambda e: list(e.path),
    )
    assert not errors, "\n".join(
        f"[{'/'.join(str(p) for p in e.path) or '<root>'}] {e.message}" for e in errors
    )


def test_schema_version_is_the_one_we_declare(manifest: dict[str, Any]) -> None:
    """If the manifest is pointed at a newer schema, the vendored copy is stale and the
    validation above is checking the wrong contract."""
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert manifest["$schema"] == schema["$id"]


def test_versions_agree_across_manifest_package_and_project(manifest: dict[str, Any]) -> None:
    """A version that disagrees with itself is rejected by the registry — after PyPI has
    accepted an immutable version number, which is the expensive moment to find out."""
    import tomllib

    from pgops import __version__

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pypi = next(p for p in manifest["packages"] if p["registryType"] == "pypi")

    assert manifest["version"] == project["version"] == __version__ == pypi["version"]


def test_container_tag_matches_the_release(manifest: dict[str, Any]) -> None:
    oci = next(p for p in manifest["packages"] if p["registryType"] == "oci")
    assert oci["identifier"].endswith(":" + manifest["version"])


# --- ownership verification -----------------------------------------------------------
# Each package type proves ownership differently. Losing a marker during an ordinary
# docs or Dockerfile edit breaks publishing in a way that is baffling after the fact.


def test_readme_carries_the_pypi_ownership_marker(manifest: dict[str, Any]) -> None:
    """The registry looks for this in the package README, which becomes the PyPI
    description. The token must not be glued to trailing punctuation."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {manifest['name']}" in readme


def test_dockerfile_carries_the_oci_ownership_label(manifest: dict[str, Any]) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f'io.modelcontextprotocol.server.name="{manifest["name"]}"' in dockerfile


def test_name_is_in_our_github_namespace(manifest: dict[str, Any]) -> None:
    """GitHub OIDC auth only grants io.github.<owner>/*; any other prefix is refused at
    publish time with a permissions error."""
    assert manifest["name"] == SERVER_NAME
    assert manifest["name"].startswith("io.github.arzharch/")


# --- the manifest has to describe a server someone can actually configure --------------


def test_every_package_declares_the_required_dsn(manifest: dict[str, Any]) -> None:
    """Clients render a configuration form from these. A required secret that is not
    declared becomes a server that silently fails to start."""
    for package in manifest["packages"]:
        env = {e["name"]: e for e in package.get("environmentVariables", [])}
        assert env["PGOPS_DSN"]["isRequired"] is True
        assert env["PGOPS_DSN"]["isSecret"] is True, "a DSN contains a password"


def test_both_install_paths_are_offered(manifest: dict[str, Any]) -> None:
    """They fail for different people: uvx needs no preinstall but assumes the host may
    run Python; the container assumes only Docker."""
    kinds = {p["registryType"] for p in manifest["packages"]}
    assert kinds == {"pypi", "oci"}


def test_pypi_entry_hints_the_runner(manifest: dict[str, Any]) -> None:
    """Without runtimeHint a client cannot show `uvx pgops-mcp`, which is the whole
    reason the PyPI entry exists."""
    pypi = next(p for p in manifest["packages"] if p["registryType"] == "pypi")
    assert pypi["runtimeHint"] == "uvx"
    assert pypi["transport"]["type"] == "stdio"
