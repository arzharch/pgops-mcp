# Security Policy

## Reporting a vulnerability

Report privately via [GitHub Security Advisories](https://github.com/arzharch/pgops-mcp/security/advisories/new).
Please do not open a public issue for anything exploitable.

Include what you did, what happened, and what you expected. A failing `query.read` or
`query.write` call is the most useful reproduction, because it maps directly onto the
guardrail that should have stopped it.

## What this server can do, by design

pgops-mcp executes SQL against a database you point it at, and — when the environment
tools are used — talks to a Docker daemon. Both are powerful by nature, so the security
model is about *which* actions are reachable, not about pretending none are:

- **The read pool sets `default_transaction_read_only = on`.** Enforcement is at the
  executor, not only in the classifier, so a statement that slips past classification
  still cannot write on the read path.
- **Deny-by-default classification** (ADR-001): anything not confidently recognised as a
  pure read is treated as the most dangerous applicable class.
- **Destructive statements require human approval** — via MCP elicitation where the
  client supports it, and via single-use, statement-bound confirmation tokens otherwise.
  The fallback is never "allow".
- **Container mutation is off unless `--approval-mode` is set.** `container.exec`
  enforces a read-only diagnostic allowlist and does not offer a shell. The Docker
  socket is root-equivalent on the host.
- **Every executed statement and every refusal is recorded** in an append-only audit log
  with the calling identity.

## Deployment notes that are security-relevant

- **stdio needs no auth and that is deliberate** (ADR-002): the server is a subprocess
  your own client spawns, with no listening port. **HTTP refuses to start without
  `--public-key`**, and binds loopback unless told otherwise.
- **Agent tokens are RS256.** The server holds only the public key, so a compromised
  server can verify tokens but never mint them. Issue the narrowest scope that works —
  `pgops:read` is the default for a reason.
- **`pgops-mcp keygen` writes a `.gitignore` into its key directory**, but do not rely
  on that alone: a private key that reaches a git history has to be treated as
  compromised even after it is removed.
- **Prefer a separate read-only role** via `PGOPS_READONLY_DSN`. Enforcement at the
  database beats enforcement in the server.
- **The audit log is the only record of who did what.** In a container, mount it on
  durable storage — an audit log that dies with the container is not an audit log.

## Known limits

Stated plainly rather than left to be discovered:

- **No per-session database isolation.** Auth identifies *who* and scopes limit *what*,
  but every authenticated caller shares one connection manager and one audit log. This
  is built for one engineer and one or a few databases, not multi-tenant SaaS.
- **Confirmation tokens are in-memory**, so a restart invalidates outstanding approvals.
  That is the safe direction to fail.
- **`container.exec`'s allowlist is checked by basename.** It is a diagnostic
  convenience, not a sandbox; treat `--approval-mode` as granting host-level trust.

## Supported versions

Pre-1.0. Fixes land on the latest release only.
