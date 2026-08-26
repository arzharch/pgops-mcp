# Changelog

Notable changes per release. Format follows [Keep a Changelog](https://keepachangelog.com/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions here are the **server's**. Nothing in `pgops.*` is a public import surface, so
SemVer describes the tool contract — tool names, arguments, return shapes, error codes,
scopes — not Python symbols.

## [Unreleased]

## [0.1.1] — 2026-08-27

First release intended for distribution. Everything below is relative to the initial
development history rather than a previous published version.

### Added
- **17 tools** across schema inspection, guarded queries, `EXPLAIN` diagnosis, migration
  planning with lock-impact analysis, and Docker environment awareness.
- **7 resources** including the `pgops://schema/{table}` template, and **5 prompts**
  encoding tool ordering that no single tool can express.
- **Elicitation** for dangerous actions — the user is asked directly rather than through
  the agent; confirmation tokens remain the fallback, never "allow".
- **Sampling**: `migration.describe` turns plain English into an analysed plan using the
  *client's* model, so this server ships no API key. Sampled output is never executed —
  the model proposes a target schema, and the deterministic planner does the rest.
- **Completions** for table names on the schema resource template.
- **HTTP transport** with RS256 agent tokens, `keygen` / `issue-token` / `scopes` CLI
  subcommands, and per-tool scope enforcement.
- **`migration.rollback`** with reversibility analysis: any irreversible step refuses the
  whole rollback rather than half-undoing it.
- **`pgops-mcp replay`** — re-classify an audit log against today's guardrails
  (a regression detector for the safety core), or re-execute it behind a typed
  confirmation.
- **Optional OpenTelemetry** spans and metrics plus `/health` and `/ready`; a no-op with
  zero configuration.
- **Per-caller tool-call rate limiting** under HTTP auth, keyed by token subject so one
  agent cannot spend another's budget. `PGOPS_RATE_LIMIT_RPS=0` disables it.
- Distribution as a **PyPI package** and a **container image**, with `server.json` for
  the MCP Registry.

### Fixed
- **Per-tool scopes were not enforced.** `TOOL_SCOPES` existed and was documented as
  authoritative, but nothing consulted it: the JWT verifier checks scopes once per
  request against a single server-wide list, so a token issued with `pgops:read` alone
  cleared the only gate there was. A read-only token was observed running `CREATE TABLE`.
- **`docker` was never declared as a runtime dependency**, reaching the environment only
  as a transitive dependency of a test package. Six tools would have failed on
  `ImportError` for anyone installing from a package index.
- **A test-only library (`hypothesis`) was a runtime dependency**, shipping to every user
  via an extra that does not exist upstream.
- **`pgops-mcp keygen` left private keys in an unguarded directory.** It now writes a
  `.gitignore` before the key itself.
- **`EXPLAIN` plan timing double-counted parallel workers**, reporting more time consumed
  than the query took.
- **`server.json`'s description exceeded the registry's 100-character limit**, which
  would have been rejected at the final step of a release — after PyPI had already
  accepted an immutable version number. Now validated against the published schema in
  the test suite.
- **The server reported no version to clients.** `serverInfo` in the MCP `initialize`
  handshake was empty, so a client had no way to detect a changed tool contract.
- **The audit log did not record the calling identity**, so an HTTP deployment could
  answer "what happened" but not "who did it".
- Documentation was double-encoded (`—` rendered as `â€"`) across six files, including
  the README that becomes the package description.

### Security
- Read pool sets `default_transaction_read_only = on`, so enforcement is at the executor
  and not only in the classifier.
- Classifier gained a `pg_proc.provolatile` lookup, closing the documented ADR-001 gap
  where a write hidden inside a volatile function was caught only at execution time.
- Container environment variables are never returned by `env.topology` — they hold
  credentials.

[Unreleased]: https://github.com/arzharch/pgops-mcp/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/arzharch/pgops-mcp/releases/tag/v0.1.1
