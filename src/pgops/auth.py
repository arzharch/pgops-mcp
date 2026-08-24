"""Authentication for HTTP transport, and token issuance for agents.

**stdio needs no auth, and adding it would be theatre.** The server is a subprocess the
user's own MCP client spawns; it inherits their privileges, listens on no port, and has
no remote caller to authenticate. The DSN comes from the client's own config. That is
why Phases 1–5 shipped with no auth at all (ADR-002) — not an oversight.

The moment the server listens on HTTP, that reasoning collapses. There *is* a remote
caller, the port is reachable, and "who is asking" becomes a real question. So auth is
tied to the transport rather than being a global setting: `--transport http` requires it
and refuses to start without it.

Scopes map to the danger tiers the rest of the project already uses, so a token can be
issued that is genuinely incapable of the thing you are worried about:

    pgops:read     schema.inspect, query.read, query.explain, db.health, index.advise,
                   migration.plan, migration.describe, migration.history, env.*,
                   container.logs/stats
    pgops:write    query.write, migration.apply
    pgops:admin    container.restart, container.exec

Read-only is the default for a newly issued token: an agent that only needs to answer
questions about a schema should not hold a credential that can drop it.

Verification is asymmetric (RS256): the server holds only the **public** key, so a
compromised server cannot mint tokens for itself or for anyone else. Issuance happens
out of band via `pgops-mcp keygen` / `pgops-mcp issue-token`, which is the "key generator
for an agent" workflow.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pgops.errors import ErrorCode, PgopsError

logger = logging.getLogger("pgops.auth")

DEFAULT_ISSUER = "https://pgops-mcp.local"
DEFAULT_AUDIENCE = "pgops-mcp"


class Scope(StrEnum):
    READ = "pgops:read"
    WRITE = "pgops:write"
    ADMIN = "pgops:admin"


# Which scope each tool requires. Anything not listed is treated as requiring ADMIN —
# deny-by-default, the same principle as the SQL classifier (ADR-001): a tool added
# later without a scope entry is locked down rather than silently public.
TOOL_SCOPES: dict[str, Scope] = {
    "schema.inspect": Scope.READ,
    "query.read": Scope.READ,
    "query.explain": Scope.READ,
    "db.health": Scope.READ,
    "index.advise": Scope.READ,
    "migration.plan": Scope.READ,
    "migration.describe": Scope.READ,
    "migration.history": Scope.READ,
    "env.topology": Scope.READ,
    "env.correlate": Scope.READ,
    "container.logs": Scope.READ,
    "container.stats": Scope.READ,
    "query.write": Scope.WRITE,
    "migration.apply": Scope.WRITE,
    "migration.rollback": Scope.WRITE,
    "container.restart": Scope.ADMIN,
    "container.exec": Scope.ADMIN,
}


@dataclass(slots=True)
class KeyMaterial:
    private_key: str
    public_key: str

    def save(self, directory: Path) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        private_path = directory / "pgops_private.pem"
        public_path = directory / "pgops_public.pem"
        private_path.write_text(self.private_key, encoding="utf-8")
        public_path.write_text(self.public_key, encoding="utf-8")
        # 0600 on POSIX. Windows ignores the mode bits, which is worth stating rather
        # than pretending the call did something it did not.
        try:
            private_path.chmod(0o600)
        except OSError:  # pragma: no cover - platform dependent
            logger.warning("could not restrict permissions on %s", private_path)
        return private_path, public_path


def generate_keypair() -> KeyMaterial:
    from fastmcp.server.auth.providers.jwt import RSAKeyPair

    pair = RSAKeyPair.generate()
    return KeyMaterial(
        private_key=pair.private_key.get_secret_value(),
        public_key=pair.public_key,
    )


def issue_token(
    private_key_pem: str,
    subject: str,
    scopes: list[str] | None = None,
    expires_in_seconds: int = 30 * 24 * 3600,
) -> str:
    """Mint a bearer token for an agent.

    `subject` identifies the agent and lands in the audit log, which is the point: with
    stdio there is exactly one caller and identity is implicit, but an HTTP server has
    many and "who ran this DELETE" must be answerable.
    """
    from fastmcp.server.auth.providers.jwt import RSAKeyPair
    from pydantic import SecretStr

    resolved = scopes or [Scope.READ.value]
    unknown = set(resolved) - {s.value for s in Scope}
    if unknown:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"unknown scopes: {sorted(unknown)}",
            hint=f"valid scopes: {[s.value for s in Scope]}",
        )

    pair = RSAKeyPair(private_key=SecretStr(private_key_pem), public_key="")
    return pair.create_token(
        subject=subject,
        issuer=DEFAULT_ISSUER,
        audience=DEFAULT_AUDIENCE,
        scopes=resolved,
        expires_in_seconds=expires_in_seconds,
    )


def build_verifier(public_key_pem: str, required_scopes: list[str] | None = None) -> Any:
    """Token verifier for the HTTP transport.

    Only the public key is held here. A server compromise therefore leaks the ability to
    *verify* tokens, not to *issue* them — which is the whole reason for choosing an
    asymmetric algorithm over a shared secret.
    """
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    return JWTVerifier(
        public_key=public_key_pem,
        issuer=DEFAULT_ISSUER,
        audience=DEFAULT_AUDIENCE,
        required_scopes=required_scopes or [Scope.READ.value],
    )


def load_public_key(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PgopsError(
            ErrorCode.INVALID_ARGUMENT,
            f"cannot read public key at {path}: {exc}",
            hint="generate one with: pgops-mcp keygen",
        ) from exc


def describe_scopes() -> str:
    return json.dumps(
        {
            "scopes": {
                Scope.READ.value: sorted(t for t, s in TOOL_SCOPES.items() if s is Scope.READ),
                Scope.WRITE.value: sorted(t for t, s in TOOL_SCOPES.items() if s is Scope.WRITE),
                Scope.ADMIN.value: sorted(t for t, s in TOOL_SCOPES.items() if s is Scope.ADMIN),
            },
            "note": "tools with no scope entry require pgops:admin (deny by default)",
        },
        indent=2,
    )
