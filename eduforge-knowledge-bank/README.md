# EduForge Knowledge Core — Milestone 1 (Foundation)

Independent knowledge-intelligence backend. **Milestone 1 delivers the foundation only** (schema, registries, auth,
API skeleton, infra). Ingestion, research, verification, retrieval and autonomous workers are *not implemented yet*
(see "Status" below). Nothing here claims 24/7 autonomous operation.

## Status — what is real

| Area | State |
|---|---|
| 19-table schema (PostgreSQL + pgvector column), soft delete, FKs, check/unique constraints | Implemented |
| Alembic baseline migration | Implemented (see "Baseline migration" in docs/ARCHITECTURE.md) |
| Domain registry (+ seed of the 10 initial domains), hierarchical topics | Implemented |
| Source registry with allow/deny/pending policy | Implemented |
| SSRF-safe URL policy (`/sources/check-url` dry-run) | Implemented (no fetching yet) |
| API-key auth + RBAC (reader/editor/admin), rate limiting, audit log, request IDs, error model | Implemented |
| Health: `/health/live`, `/health/ready` (PostgreSQL + Redis) | Implemented |
| Docker Compose (db, redis, migrate, api) | Written, **not yet run by the author** |
| Document ingestion (PDF/HTML/TXT/MD/JSON), async worker, normalized content + SHA-256 dedup | Implemented (text-based; OCR not included) |
| Persistent knowledge chunks with document/page provenance | Implemented (Milestone 3 foundation) |
| Explainable lexical retrieval API (`POST /knowledge/retrieve`) | Implemented baseline; semantic/vector retrieval is next |
| Autonomous research, AI explanations, verification, embeddings, self-improvement agents | **Not implemented yet** |

**Verification honesty:** the build environment had no network, so dependencies could not be installed and the
pytest suite and Docker stack were **not executed** there. Only the stdlib-only parts (SSRF URL policy cases,
in-memory rate limiter logic) were run. Run the commands below on your machine and treat any failure as a bug to fix.

## Run with Docker

```bash
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD (also inside DATABASE_URL) and the three API keys
python -c "import secrets; print(secrets.token_urlsafe(32))"   # run 3 times for the keys

docker compose up --build -d
docker compose ps                      # db/redis healthy, migrate exited 0, api healthy
curl localhost:8000/health/live
curl localhost:8000/health/ready       # {"status":"ok","checks":{"database":"ok","redis":"ok"}}

# seed the 10 initial domains
docker compose run --rm api python -m app.scripts.seed_domains
```

Interactive docs (non-production): http://localhost:8000/docs

## Run tests (no Docker needed; SQLite in memory)

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

## Run locally without Docker for the API

Start only the services: `docker compose up -d db redis`, set `DATABASE_URL`/`REDIS_URL` host to `localhost` in
`.env`, then:

```bash
alembic upgrade head
python -m app.scripts.seed_domains
uvicorn app.main:app --reload
```

## Try every endpoint

Authentication: header `X-API-Key`. Roles: **reader** (GET) < **editor** (create/update, delete topics) <
**admin** (delete domains/sources, change source policy, read audit logs).

```bash
H='-H Content-Type:application/json'
ED="-H X-API-Key:$EDITOR_API_KEY"; AD="-H X-API-Key:$ADMIN_API_KEY"; RD="-H X-API-Key:$READER_API_KEY"
B=localhost:8000/api/v1

# Domains
curl $B/domains $RD                                                     # list (page, page_size, is_active)
curl -X POST $B/domains $H $ED -d '{"slug":"astronomy","name":"Astronomy"}'
curl $B/domains/<domain_id> $RD
curl -X PATCH $B/domains/<domain_id> $H $ED -d '{"description":"Stars and planets"}'
curl -X DELETE $B/domains/<domain_id> $AD                                # soft delete; 409 if it has topics

# Topics (hierarchical; path is built from slugs)
curl -X POST $B/topics $H $ED -d '{"domain_id":"<domain_id>","slug":"algebra","name":"Algebra"}'
curl -X POST $B/topics $H $ED -d '{"domain_id":"<domain_id>","parent_id":"<topic_id>","slug":"linear-equations","name":"Linear equations"}'
curl "$B/topics?domain_id=<domain_id>&roots_only=true" $RD
curl -X PATCH $B/topics/<topic_id> $H $ED -d '{"name":"Algebra I"}'
curl -X DELETE $B/topics/<topic_id> $ED                                  # 409 if it has subtopics

# Sources (start as PENDING; only admin can ALLOW/DENY)
curl -X POST $B/sources $H $ED -d '{"name":"Example Press","base_url":"https://example.org","authority_score":0.7}'
curl "$B/sources?policy=PENDING" $RD
curl -X PATCH $B/sources/<source_id> $H $AD -d '{"policy":"ALLOWED"}'
curl -X POST $B/sources/check-url $H $ED -d '{"url":"https://example.org/page"}'   # dry-run, fetches nothing
curl -X DELETE $B/sources/<source_id> $AD

# Audit trail (admin)
curl "$B/audit-logs?entity_type=source&action=source.update" $AD
```

Every error looks like `{"error": {"code": "...", "message": "...", "details": ...}}`.

## Layout

```
app/
  core/    config, logging, errors, security (API keys + RBAC), rate_limit, url_policy (SSRF)
  db/      declarative base + mixins, engine/session
  models/  one module per aggregate (domain, source, claim, knowledge, ops) + enums
  schemas/ Pydantic v2 request/response models
  services/ business logic (domains, topics, sources, audit, seed) — API layer stays thin
  api/     health + /api/v1 routers (domains, topics, sources, audit-logs)
  infra/   Redis client
  scripts/ seed_domains
migrations/ Alembic (0001 baseline)      tests/ pytest suite      docs/ARCHITECTURE.md
```

## Knowledge chunks and retrieval (Milestone 3 foundation)

Successful ingestion now stores each extracted passage in `knowledge_chunks`, separately from the original document.
PDF passages retain 1-based page ranges; every chunk stores its document ID, content hash, language, and token estimate.
The first retrieval endpoint is deliberately extractive and explainable: it ranks matching passages and returns source/page
metadata instead of generating unsupported answers. It is a lexical baseline, not semantic search or an AI tutor.

```bash
curl -X POST http://localhost:8000/api/v1/knowledge/retrieve \
  -H 'Content-Type: application/json' -H 'X-API-Key: YOUR_READER_KEY' \
  -d '{"query":"explain photosynthesis","limit":5}'
```

Apply the new migration with `alembic upgrade head` before starting the updated API/worker.

## Known limitations (Milestone 1-3)

- Tests and Docker stack not executed by the author (no network in the build sandbox) — see "Verification honesty".
- Baseline migration uses model metadata (`create_all`); convert to explicit before first production deploy.
- Dependencies are version *ranges*; generate a lock file (`pip-compile`) once you can install packages.
- Rate limiting is per role+client IP fixed window; keys are static API keys (no user accounts / JWT yet).
- `knowledge_versions` immutability is enforced by service design in later milestones, not yet by a DB trigger.
- No worker/queue yet: no Celery, no scheduled jobs, no dead-letter queue.
- Embedding dimension fixed at 1536 (change requires a migration + re-embedding).

## Next: Milestone 4 — Knowledge intelligence

Add a configurable LLM provider, claim/concept extraction, subject/topic classification, source-backed explanations,
contradiction checks, and semantic embeddings/retrieval. External research must use the approved-source registry and
SSRF-safe fetch policy; no unrestricted URL fetching or autonomous publishing is enabled.

## Historical: Milestone 2 — Knowledge Ingestion

Document intake (HTML/PDF/TXT/MD/JSON), text extraction, normalization, hashing + deduplication, persistent
ingestion jobs on a Celery/Redis queue with retries, idempotency and dead-letter handling.

## Document ingestion (Milestone 2 — initial implementation)

The API accepts editor/admin uploads for PDF, HTML, TXT, Markdown and JSON. It stores the original file in the configured persistent storage directory, creates an `ingestion_jobs` row, and queues extraction on Celery/Redis. The worker extracts and normalizes text, computes a SHA-256 content hash, and skips duplicate normalized content. A supplied source must already be explicitly `ALLOWED`; uploads without a source are accepted as user-provided documents.

```bash
# Start API and the ingestion worker
docker compose up --build -d

# Upload a document (replace the key)
curl -X POST http://localhost:8000/api/v1/documents/ingest \\
  -H 'X-API-Key: YOUR_EDITOR_OR_ADMIN_KEY' \\
  -F 'file=@./lesson.pdf'

# Poll the returned job_id
curl http://localhost:8000/api/v1/documents/jobs/JOB_ID \\
  -H 'X-API-Key: YOUR_READER_OR_HIGHER_KEY'
```

Supported extraction is text-based. Scanned/image-only PDFs require OCR and are not yet interpreted. URL fetching is deliberately not part of this upload endpoint; remote source collection will be implemented separately behind the source allowlist and SSRF controls. Worker tasks use late acknowledgements, retries (three total attempts), and the persistent ingestion job record. For production, mount durable storage and configure backups for both PostgreSQL and the document volume.

## Educational knowledge bank (Milestone 2)

The bank is curriculum-focused, not a general-purpose encyclopedia. It stores reusable
teaching assets for school and university subjects: explanations, definitions, worked
examples, formulas, exercises, quizzes, summaries, glossaries and lesson plans.
The application is the presentation layer; it requests approved assets and handles
ordering, layout and PDF design.

### Learning asset API

- `POST /api/v1/learning/assets` (editor): add a learning asset. New assets default to `DRAFT`.
- `GET /api/v1/learning/assets` (reader): browse approved assets by subject, grade and type.
- `POST /api/v1/learning/pack` (reader): request a ready bundle for an education system,
  stage, grade, subject, unit and lesson. The response reports `READY`, `PARTIAL` or
  `NOT_READY`, and explicitly lists missing asset types.

Example pack request:

```json
{
  "education_system": "Egypt",
  "stage": "Preparatory",
  "grade": "Third Preparatory",
  "subject": "Mathematics",
  "unit": "Algebra",
  "lesson": "Linear Equations",
  "include": ["explanation", "example", "exercise", "summary"],
  "language": "ar"
}
```

Content is not silently invented or auto-approved. Assets must be added and approved
through a trusted editorial/research workflow before the pack endpoint returns them.
Automated web research, AI authoring, evidence verification, scheduled refresh and
semantic retrieval are subsequent milestones and are not claimed as implemented yet.
Apply the new migration with `alembic upgrade head` before using these endpoints.

## AI-assisted educational authoring (Stage 3)

The knowledge bank can now create an AI-authored educational **draft** through
`POST /api/v1/learning/generate` (EDITOR role). Configure an OpenAI-compatible
Chat Completions endpoint with `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, and
`LLM_TIMEOUT_SECONDS`. The provider is optional; without a key the endpoint
returns HTTP 503 and the rest of the bank continues to work.

Generated material is always stored as `DRAFT`, tagged `ai-generated` and
`needs-review`, with `quality_score=0`. It is not delivered by `/learning/pack`
until an administrator approves it. The model is explicitly instructed not
to invent sources or claim it searched the web. Supplied source references are
preserved, but this stage does not perform independent web research or
fact-checking. Configure the provider and review generated content before
production use.

Example request:

```json
{
  "education_system": "Egypt",
  "stage": "Preparatory",
  "grade": "Third Preparatory",
  "subject": "Mathematics",
  "unit": "Algebra",
  "lesson": "Linear equations",
  "asset_type": "explanation",
  "language": "ar",
  "learner_level": "beginner",
  "learning_objectives": ["Understand the meaning of an equation", "Solve a one-step equation"]
}
```


## Milestone 4 — Sequential educational agent pipeline

`POST /api/v1/learning/agent-pipeline` runs four configured model calls in sequence:
1. Research Agent: summarizes matching knowledge already stored in the bank and identifies evidence gaps.
2. Analysis Agent: creates a prerequisite/concept/misconception teaching plan.
3. Teacher Agent: authors the requested learning asset.
4. Verification Agent: critiques the draft against the supplied bank evidence.

The result is saved as `DRAFT` and cannot be delivered as approved content until an administrator reviews it. The endpoint uses the existing OpenAI-compatible `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, and timeout settings.

**Important boundaries:** this milestone does not crawl the public internet, autonomously add unreviewed facts, train/fine-tune a model, or self-publish. The Research Agent currently searches the bank's existing document chunks and approved learning assets only (lexical retrieval). Verification is an AI review aid, not a guarantee of correctness. External web research, semantic/vector retrieval, source fetching from the approved registry, and scheduled gap-filling are future milestones.

Example:
```bash
curl -X POST http://localhost:8000/api/v1/learning/agent-pipeline \
  -H 'Content-Type: application/json' -H 'X-API-Key: YOUR_EDITOR_KEY' \
  -d '{"education_system":"Egypt","stage":"Preparatory","grade":"Third preparatory","subject":"Mathematics","lesson":"Linear equations","asset_type":"explanation","language":"ar","learner_level":"beginner"}'
```

## Knowledge Growth Engine (Stage 5)

The growth module can inspect approved curriculum assets and report missing asset types
(explanation, example, exercise, quiz, summary, glossary). `POST /api/v1/growth/scan`
creates idempotent persistent `ResearchTask` records for those gaps. Tasks can be listed
with `GET /api/v1/growth/tasks` and researched explicitly with
`POST /api/v1/growth/tasks/{task_id}/research`.

External search is optional and uses Tavily when `TAVILY_API_KEY` is configured. Search
results are stored as **unverified leads**, not approved knowledge. They must be ingested,
checked against reliable sources, and reviewed before educational assets can be approved.
The scan queues tasks; it does not run continuously by itself. A scheduled worker is a
separate deployment step and must be enabled deliberately.


## Knowledge Auto-Learning Engine (Stage 6)

Stage 6 connects research discovery to bounded source ingestion and a persistent
background schedule:

- A research task searches Tavily for candidate educational pages when `TAVILY_API_KEY`
  is configured. Search snippets remain leads, not evidence.
- A candidate page is fetched only when its hostname is registered in `sources` and its
  policy is explicitly `ALLOWED`. DNS/IP URL policy is checked before each fetch.
- Redirects are not followed; only HTML/XHTML/plain-text/Markdown are accepted; response
  bodies are streamed with a 3 MiB ceiling. The page is stored as a `SourceSnapshot`,
  `RawDocument`, and provenance-tagged `KnowledgeChunk` records.
- All fetched content is marked `UNVERIFIED`; it is not converted into approved learning
  assets and cannot be returned by the approved learning-pack API until reviewed.
- A Celery Beat scheduler queues curriculum gap scans daily and drains research tasks
  every five minutes. The worker now listens on both `ingestion` and `research` queues.

For automatic operation, set `TAVILY_API_KEY`, register relevant educational domains in
`/api/v1/sources`, and explicitly change their source policy to `ALLOWED` after review.
Then run the stack with `docker compose up -d --build`; the `worker` and `scheduler`
services are included. If Tavily is not configured, scheduled research attempts will fail
with a configuration error and remain visible in the task history.

**Safety and quality boundary:** this is automated discovery and source capture, not
autonomous truth certification or model self-training. DNS is validated before requests,
but production deployments should additionally use an outbound network firewall / egress
proxy to mitigate DNS-rebinding and infrastructure-level SSRF risks. Human/editorial
verification remains required before content is approved for students.

## Stage 8 — integrated verification, hybrid retrieval and `/knowledge/prepare`

New: `app/services/verification.py` (claim verification), `app/services/prepare.py` + `preparation_jobs.py` (workflow and
persistent jobs), `app/agents/` (schema-validated Research/Analysis/Teacher/Verification agents), hybrid retrieval in
`app/services/retrieval.py`, embeddings in `app/services/embeddings.py`, migration `0005`, and the pure engines in `app/engines/`
(Arabic normalization, SymPy checks, claim extraction, deterministic linear-equation generator, RRF fusion, conflict flagging).
API reference: **docs/API_PREPARE.md**.

### Honest status
| Item | State |
|---|---|
| Pure engines (Arabic text, SymPy verification, claims, generator, fusion, conflicts) | Implemented; **23 unit tests executed and passing** (minimal runner, not pytest) |
| Verification service, prepare workflow, jobs, agents, hybrid retrieval, API endpoints, migration 0005 | Implemented; byte-compiled and name-checked only — **not executed** (authoring sandbox had no network, so FastAPI/SQLAlchemy could not be installed) |
| `tests/test_prepare_integration.py` (23 API+DB tests on SQLite) | Written, **never run** — run `pytest` first; expect to fix some failures |
| `tests/test_postgres_integration.py` (Alembic, FTS, pgvector) | Written, skipped without `TEST_DATABASE_URL`, **never run** |
| Docker, PostgreSQL, Redis/Celery, real LLM, embeddings, Tavily | **Not tested** (credentials/infrastructure unavailable) |

Known limits: the deterministic generator covers only linear equations in one unknown; text claims are checked by lexical
overlap (not semantic entailment); no reranker; `formulas`/`summary` need the async LLM path; subject/country matching is
normalized-string based (no Arabic↔English subject translation beyond a small alias list); the LLM test double only replaces
the HTTP transport.


## Stage 9 — Curriculum Intelligence, Coverage, Knowledge Graph, Quality & Continuous Learning
Four integrated engines on top of Stage 8 (see **ARCHITECTURE.md**, **docs/API_STAGE9.md**, **FINAL_REPORT.md**):
curriculum hierarchy with versions and context resolution; coverage reports with configurable completeness profiles;
a typed, validated, auditable knowledge graph; explainable quality assessment and a bounded, reviewable continuous-learning loop.
`/knowledge/prepare` now resolves the curriculum lesson, links drafts into the graph and returns the lesson's coverage state.

Run everything that does not need infrastructure: `python -m pytest tests/engines` (pure rules, no dependencies except SymPy).
Full suite: `pip install -r requirements-dev.txt && pytest`. PostgreSQL/pgvector/Alembic tests:
`TEST_DATABASE_URL=postgresql+psycopg://... pytest tests/test_postgres_integration.py`. Migrations: `alembic upgrade head` (0006).
**Status of verification is in FINAL_REPORT.md — most Stage 8/9 database and API tests have not been executed yet.**
