# Architecture Decision Records

One file per decision, numbered in the order they were taken. Each records the context,
the decision, and the consequences — including the ones we did not like.

| ADR | Decision |
|---|---|
| [ADR-001](ADR-001.md) | Deny-by-default SQL classifier |
| [ADR-002](ADR-002.md) | stdio transport first, HTTP later |
| [ADR-003](ADR-003.md) | Own migration ledger over external tools |
| [ADR-004](ADR-004.md) | Lock-impact analysis via heuristics + known-safe patterns |
| [ADR-005](ADR-005.md) | testcontainers-based integration tests, no guardrail mocks |
| [ADR-006](ADR-006.md) | Scaling path — local-first core, shared-service tier later |

An ADR is never edited to reflect a change of mind. If a decision is reversed, a new ADR
supersedes it and the old one is marked `Superseded by ADR-NNN` — the reasoning that
looked right at the time is the part worth keeping.
