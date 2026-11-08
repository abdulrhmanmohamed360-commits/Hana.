# EduForge Knowledge Core — Architecture (Milestone 1)

## 1. Principles
Backend-only; evidence and provenance first; modular engines behind explicit interfaces; no fake AI; nothing is
marked VERIFIED without a verification workflow; every state change is audited; external content is untrusted data.

## 2. Target architecture (final)

```
            clients (future EduForge app, admin tools)
                         │  REST /api/v1  (API key → RBAC, rate limit, audit)
                   ┌─────▼──────┐
                   │  FastAPI   │  thin routers → services
                   └─────┬──────┘
   ┌───────────┬─────────┼──────────┬────────────┬───────────────┐
 Ingestion  Research   Analysis  Verification  Retrieval     Evolution      ← engines (separate modules, M2-M7)
   └───────────┴─────────┴────┬─────┴────────────┴───────────────┘
                     Agent Orchestrator (structured tasks, retries, idempotency)
                              │
              Redis (queues, cache, rate limits)   Celery workers + beat (M2, M7)
                              │
              PostgreSQL + pgvector (system of record: sources → documents → claims → evidence → versioned knowledge)
```

Milestone 1 builds the bottom and the edge: schema, registries, security, API skeleton, infra. Engines arrive in
Milestones 2-7; production hardening in 8.

## 3. Key decisions
| Decision | Why |
|---|---|
| Sync SQLAlchemy 2 + psycopg 3 | Celery workers are sync; simpler transactions; FastAPI runs sync routes in a threadpool. Revisit only if profiling demands async. |
| Services layer between API and DB | Keeps engines/agents able to call the same logic without HTTP. |
| Enums stored as VARCHAR (non-native) | Adding states never needs `ALTER TYPE` migrations. |
| Soft delete + *partial* unique indexes (`WHERE deleted_at IS NULL`) | Slugs/hosts reusable after deletion; history retained. |
| Knowledge = `knowledge_items` (identity) + append-only `knowledge_versions` (+ `is_current` with one-current partial unique index) | Old versions are never overwritten; traceable via `supersedes_version_id` and `source_claim_ids`. |
| Claim vs evidence separation, `independence_group` on evidence | Lets Milestone 5 count copies of one origin as a single independent vote. |
| Source `policy` PENDING/ALLOWED/DENIED; only admin changes it | Allowlist/denylist is data, auditable. New sources are never fetchable by default. |
| SSRF policy in pure-stdlib module | Small, auditable, unit-testable; resolves DNS and rejects if *any* address is non-public. |
| API keys per role from env vars, constant-time compare, disabled if unset | Simple secure default now; JWT/users can replace `get_principal` without touching routers. |
| JSONB only for `meta`/payload columns | Core facts live in real columns. |
| Portable types (`with_variant`) | Fast SQLite unit tests; PostgreSQL is the real target. |

## 4. Schema summary (18 tables)
- **Registry:** `knowledge_domains`, `topics` (self-FK tree, `path` unique per domain), `concepts`
- **Provenance:** `sources` (host unique, authority 0-1, policy), `source_snapshots` (immutable fetch records, unique source+url+hash), `raw_documents` (unique `content_hash` → dedupe)
- **Claims & evidence:** `extracted_claims` (status RAW/EXTRACTED/PENDING_VERIFICATION/VERIFIED/CONFLICTED/REJECTED/OUTDATED), `evidence_records` (stance, authority, relevance, independence_group, methodology)
- **Knowledge:** `knowledge_items`, `knowledge_versions`, `knowledge_relationships` (8 relation types, no self-relations), `embeddings` (pgvector `vector(1536)`)
- **Operations:** `research_tasks`, `ingestion_jobs`, `verification_jobs` (shared state machine: status, priority, attempts, max_attempts, scheduled_at, last_error, unique idempotency_key), `agent_runs` (tokens, cost), `audit_logs` (append-only), `domain_quality_metrics`

## 5. Security in Milestone 1
Env-only secrets; API-key auth + RBAC; per-key+IP rate limit (Redis, fail-open with warning; memory in dev);
pydantic validation everywhere; SSRF rules (scheme, credentials, local names, private/link-local/CGNAT/mapped-IPv6,
DNS-resolved checks); audit row written in the same transaction as each change; docs/openapi disabled in production;
non-root container. Prompt-injection defences (treat documents as data) belong to the analysis/agent milestones.

## 6. Baseline migration
`0001_initial_schema` runs `CREATE EXTENSION vector` then builds tables from model metadata. Before the first
production deploy, make it explicit once:
```bash
docker compose up -d db
dropdb/recreate empty database; alembic revision --autogenerate -m "explicit baseline"   # on an empty DB
# review the generated file, replace 0001 with it, then: alembic upgrade head
```
All later migrations must be explicit autogenerate scripts, reviewed by hand.

## 7. Backup & recovery (initial)
Postgres is the system of record. Minimum: nightly `pg_dump -Fc` + WAL archiving for point-in-time recovery,
Redis AOF is already enabled (queues are re-derivable from `*_jobs` rows once workers exist). Restore drill and
automation are scheduled for Milestone 8 — not yet implemented or tested.

## 8. Risks / open points
- Docker stack and tests unverified in the build sandbox → run and fix.
- Embedding dimension is fixed; switching providers with different dimensions needs a migration.
- Static API keys are coarse; plan user accounts/JWT before exposing beyond internal clients.
- No DB-level immutability trigger on `knowledge_versions` / `audit_logs` yet (planned in M5/M8).
- Legal/ToS compliance for crawling is enforced by policy data + robots checks in Milestone 3, not by code today.

## 9. Roadmap
M2 Ingestion · M3 Research · M4 Analysis · M5 Verification · M6 Retrieval (FTS + pgvector hybrid) ·
M7 Autonomous evolution (gap detection, schedules, reverification) · M8 Hardening (security review, load test,
monitoring, backup/restore drills).
