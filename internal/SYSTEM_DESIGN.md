# System Design â€” pgops-mcp

Rendered architecture diagrams. The source of truth for *why* each component exists is
[`ARCHITECTURE.md`](ARCHITECTURE.md); this page is the visual companion.

---

## 1. High-level system view

```mermaid
graph TB
    subgraph Clients["MCP Clients"]
        CD["Claude Desktop"]
        CUR["Cursor / VS Code"]
        INS["MCP Inspector"]
        AG["Custom agents<br/>(HTTP + JWT)"]
    end

    subgraph Transport["Transport layer"]
        STDIO["stdio<br/>(local subprocess,<br/>no auth needed)"]
        HTTP["Streamable HTTP<br/>(RS256 JWT required,<br/>loopback by default)"]
    end

    subgraph Server["pgops-mcp server (FastMCP 3.x, Python 3.12+, async)"]
        direction TB
        MW["ScopeEnforcement middleware<br/>per-tool scope check Â· deny-by-default"]

        subgraph Surface["MCP surface"]
            TL["15 Tools<br/>schema.inspect Â· query.read/write/explain<br/>index.advise Â· db.health<br/>migration.plan/apply/rollback/history/describe<br/>env.topology/correlate Â· container.logs/stats/restart/exec"]
            RES["7 Resources<br/>pgops://schema Â· /health Â· /migrations<br/>/audit/recent (redacted) Â· /config (no DSN)"]
            PRM["5 Prompts<br/>diagnose-slow-query Â· plan-safe-migration<br/>incident-triage Â· review-index-health<br/>explain-safety-model"]
            ELI["Elicitation<br/>asks the USER directly<br/>token fallback if unsupported"]
        end

        subgraph Safety["Safety core"]
            CL["Classifier<br/>read/write/ddl/destructive/unknown<br/>deny-by-default (ADR-001)"]
            GR["Guardrails<br/>unbounded-mutation block<br/>confirmation tokens (single-use,<br/>statement-bound, TTL 5min)"]
            APPR["Approval engine<br/>elicitation â†’ token fallback<br/>never falls back to 'allowed'"]
        end

        subgraph Engines["Domain engines"]
            ME["Migration Engine<br/>diff â†’ plan â†’ lock analysis<br/>â†’ dry-run â†’ apply â†’ rollback<br/>ledger: pgops_migrations"]
            PB["Performance Brain<br/>EXPLAIN JSON parser<br/>6 verdict rules<br/>pg_stat_statements advisor"]
            ENV["Environment Layer<br/>Docker SDK via asyncio.to_thread<br/>field allowlist (no secrets)"]
        end

        CM["ConnectionManager<br/>readonly pool (eager, PG-enforced RO)<br/>readwrite pool (lazy)"]
        AU["Audit Log<br/>append-only JSONL, fsync per entry<br/>actor identity recorded"]
    end

    subgraph Infra["Local infrastructure"]
        PG[("PostgreSQL 16<br/>pg_stat_statements")]
        DK["Docker daemon socket<br/>(root-equivalent â€” read-only API default)"]
        APP["App containers"]
        FS[("Local filesystem<br/>~/.pgops/audit.jsonl")]
    end

    CD & CUR & INS --> STDIO --> MW
    AG --> HTTP --> MW
    MW --> TL & RES & PRM
    TL --> CL --> GR --> APPR
    APPR --> CM
    TL --> ME & PB & ENV
    GR & ME & ENV --> AU --> FS
    CM -->|"readonly role<br/>SET default_transaction_read_only"| PG
    ME -->|"readwrite role, txns"| PG
    PB -->|"catalog + stats queries"| PG
    ENV -->|"read-only API"| DK
    DK --- APP
```

---

## 2. Safety pipeline â€” every write goes through this

```mermaid
flowchart LR
    A["Tool call<br/>(query.write / migration.apply /<br/>migration.rollback / container.*)"] --> B{"Classifier<br/>deny-by-default"}
    B -->|unknown| C["Treat as destructive"]
    B -->|destructive / unbounded| D{"Elicitation<br/>supported?"}
    C --> D
    D -->|yes| E["Ask the USER directly<br/>(outside the model's turn)"]
    E -->|approve| F["Execute on readwrite pool"]
    E -->|decline| G["CONFIRMATION_DECLINED<br/>NO token issued"]
    D -->|no| H["Issue confirmation token<br/>bound to sha256(sql)<br/>single-use, TTL 5 min"]
    H --> I["Agent relays reason to human"]
    I --> J["Re-call with token"]
    J --> K{"Token valid,<br/>unexpired,<br/>statement matches?"}
    K -->|yes| F
    K -->|no| L["CONFIRMATION_MISMATCH<br/>token NOT consumed"]
    B -->|safe read| M["Execute on readonly pool<br/>(PG-enforced read-only)"]
    F --> N["Audit entry:<br/>sql, hash, duration, rows,<br/>verdict, actor, approval method"]
    G --> N
    L --> N
    M --> O["Audit entry"]
```

**The load-bearing property:** approval never degrades to "allowed". Losing elicitation
downgrades "the human was asked" to "the agent asserts the human was asked" â€” weaker,
but still gated.

---

## 3. Migration engine data flow

```mermaid
flowchart TB
    T["Target schema (JSON desired-state)<br/>or English description â†’ sampling"] --> V{"_validate_target"}
    V -->|invalid| ERR1["INVALID_ARGUMENT<br/>(unsupported keys refused,<br/>never silently skipped)"]
    V -->|valid| DIFF["schema.diff(live, target)<br/>dependency-ordered change set<br/>creations outside-in, drops reversed<br/>allow_drops=false by default"]
    LIVE["schema.inspect(level=full)"] --> DIFF
    DIFF --> LA["Lock analysis per step<br/>op class Ã— table size â†’ estimate<br/>+ confidence + reasoning<br/>constant vs volatile DEFAULT split"]
    LA --> DR{"dry_run?"}
    DR -->|yes| TXN["Execute transactional steps<br/>inside a doomed transaction<br/>(always rolled back)"]
    TXN --> PLAN["MigrationPlan<br/>plan_id Â· checksum Â· steps annotated<br/>atomic flag Â· notes"]
    DR -->|no| PLAN
    PLAN --> APPLY{"migration.apply"}
    APPLY --> CK{"checksum match?<br/>in_flight stranded?"}
    CK -->|stale or stranded| ERR2["Refuse:<br/>MIGRATION_IN_FLIGHT"]
    CK -->|clean| CONF{"destructive or<br/>high-risk?"}
    CONF -->|yes| TOK["Confirmation token flow<br/>(same as safety pipeline)"]
    CONF -->|no| EXEC
    TOK --> EXEC["Ledger row: in_flight FIRST<br/>â†’ transactional apply<br/>â†’ ledger: applied | failed"]
    EXEC --> RB["migration.rollback available:<br/>reversible / data-loss / irreversible<br/>irreversible refuses whole rollback,<br/>issues NO token"]
```

---

## 4. Deployment topology â€” what scales and what doesn't

```mermaid
flowchart LR
    subgraph T1["Tier 1 Â· Local (shipped)"]
        direction TB
        C1["One MCP client<br/>(stdio)"] --> S1["One pgops-mcp process"] --> D1["User's Postgres"]
        S1 --> A1["Local audit file<br/>~/.pgops/audit.jsonl"]
    end

    subgraph T2["Tier 2 Â· Shared team server (designed for)"]
        direction TB
        C2a["Agent A"] & C2b["Agent B"] & C2c["Agent C"] -->|"HTTP + JWT<br/>scoped tokens"| S2["pgops-mcp instance"]
        S2 --> D2["Shared dev/staging Postgres<br/>+ per-session DSN isolation"]
        S2 --> R[("Redis: plan cache,<br/>confirmation tokens")]
        S2 --> A2["Centralized audit sink<br/>(OTel / log pipeline)"]
    end

    subgraph T3["Tier 3 Â· Hosted multi-tenant (boundary, not roadmap)"]
        direction TB
        C3["Many customers"] --> S3["Per-tenant isolation<br/>network, keys, compliance"] --> D3["Tenant databases"]
    end

    T1 ==>|"add: DSN isolation,<br/>audit sink, external state,<br/>rate limits"| T2
    T2 -.->|"different product:<br/>compliance surface,<br/>per-tenant Docker boundaries"| T3
```

### Honest scaling assessment

This is **a local-first developer tool, not a horizontally-scaled service** â€” and that
is a design decision, not an omission. The full tier analysis lives in
[ADR-006](adr/ADR-006.md); the summary:

| Dimension | Status | Why |
|---|---|---|
| Concurrency within one server | âœ… fine | async pools; readonly pool sized 5, readwrite deliberately 2 |
| Multiple clients, one server | âš ï¸ works with caveats | shared audit log, serialized writes â€” documented limitation |
| Multi-tenant (many users, many DBs) | âŒ not built | every authenticated caller shares one `ConnectionManager`; scoped tokens limit *what*, not *which database* |
| Horizontal scale-out | âŒ not built | plan cache and token store are **in-memory**, so instances can't share state |
| High availability | âŒ not applicable | it's an operator's sidecar, not a service with an SLA |

**The case for keeping Tier 1 as the default** â€” worth stating because it's a security
argument, not a limitation: when the database is local, the safest place for the operator
credential is the user's own machine, under their own account, with **no network listener
at all**. Moving to Tier 2 buys team collaboration at the price of a network attack
surface that must then be defended forever. The transport-bound auth design (stdio needs
no auth *because* there is no remote caller; HTTP refuses to start without one) means
that trade gets made deliberately per deployment rather than being forced on everyone.

What the design already gives the scale-up path:

- **Transport-agnostic tool layer** (ADR-002): HTTP shipped behind JWT auth in Phase 6b;
  multi-client support doesn't touch tools.
- **Stateless-friendly auth**: RS256 public-key verification â€” any instance verifies
  tokens with no shared secret.
- **Content-addressed ephemeral state**: tokens are bound to `sha256(sql)` and plans to
  checksums with TTLs, so moving them to Redis later is mechanical.

The two real blockers are named deliverables now (ADR-006): per-session DSN isolation
and a centralized audit sink. Both are gaps by decision, not oversight.
