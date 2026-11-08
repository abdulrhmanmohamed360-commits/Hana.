# EduForge Knowledge Bank — Architecture (Stage 9)

The Knowledge Bank owns the educational intelligence; the EduForge application only requests, arranges and exports content.
FastAPI + SQLAlchemy 2 + PostgreSQL (pgvector) + Celery/Redis + Pydantic. Older notes: `docs/ARCHITECTURE.md` (Stage 1-6).

## Layers
```
app/api/v1/*        thin HTTP layer: auth (X-API-Key roles reader<editor<admin), validation, commit, JSON
app/schemas/*       Pydantic contracts (public API + strict agent I/O)
app/services/*      all database logic (one module per capability)
app/agents/*        schema-validated LLM runner and agent contracts
app/engines/*       PURE Python rules (no DB / no framework) — the part that is unit-tested without infrastructure
app/models/*        SQLAlchemy models;  migrations/versions/*  Alembic (0001..0006)
app/tasks.py        Celery tasks + beat schedule
```

## The four Stage 9 systems and how they connect
```
 Curriculum Intelligence ──structure──▶ Knowledge Coverage ──gaps──▶ Continuous Learning ──drafts──▶ human review
   curricula, curriculum_nodes            coverage_profiles            knowledge_gaps, scan_runs,         (admin approves)
   resolve_context()                      lesson_state()               scan_schedules, research tasks
        │  ▲                                   ▲                              │
        │  └────────── Knowledge Graph (graph_edges) ◀────────────────────────┤
        ▼              PREREQUISITE_OF, PART_OF, EXPLAINS, HAS_EXAMPLE ...     ▼
   /knowledge/prepare ──────────────▶ Knowledge Quality (quality_assessments, explainable dimensions)
```
* **Curriculum Intelligence** (`services/curriculum.py`, `engines/curriculum_rules.py`): Country/System live on `curricula`
  (versioned); Stage→Grade→(Year)→(Term)→Subject→(Unit)→Lesson→(Topic)→Concept are rows of one adjacency table
  `curriculum_nodes`, placement-validated, duplicate-proof via an Arabic-normalised `name_key` + partial unique indexes.
  `resolve_context()` maps a request (country/grade/term/subject/lesson) to exactly one lesson or reports AMBIGUOUS / NOT_FOUND.
  New versions deep-copy structure; activating one archives the old and carries unchanged lessons' content over.
* **Knowledge Coverage** (`services/coverage.py`, `engines/coverage_rules.py`): per-lesson requirements (type, count, min length,
  sources, answers) from built-in or admin-defined `coverage_profiles`. Only APPROVED content counts; drafts → PENDING_REVIEW;
  old approved content → OUTDATED. Also reports concepts without explanation, formulas without examples, exercises without
  answers, content without sources, unverified claims.
* **Knowledge Graph** (`services/graph.py`, `engines/graph_rules.py`): `graph_edges` links nodes, assets, claims and sources with the
  ten required relation types. Endpoint-kind rules, de-duplication (symmetric relations canonicalised), cycle prevention for
  PREREQUISITE_OF / PART_OF / EXTENDS, provenance + creator + timestamp, soft delete, every change audited. Traversals are
  depth-limited frontier queries (never load the graph). The older `knowledge_relationships` table (concept↔concept only) is kept.
* **Quality & Continuous Learning** (`services/quality.py`, `services/continuous_learning.py`, `engines/quality_rules.py`):
  seven explainable dimensions; `support_score` (sourcing/verification/consistency/freshness/review) is kept separate from
  `correctness` (only deterministic checks can yield DETERMINISTICALLY_VERIFIED). Scans detect gaps, source changes and conflicts, plan
  *bounded* research (per-scan and per-day caps; needs a search provider), and can ask the preparation workflow for DRAFT content.

## Integration with the Stage 8 preparation workflow (`services/prepare.py`)
`POST /knowledge/prepare` now (1) resolves the curriculum lesson, (2) also finds approved assets mapped to that node,
(3) links every generated draft to the lesson and creates typed graph edges, (4) returns `curriculum` (path, objectives,
prerequisites, missing prerequisites) and `knowledge_state` (coverage after the request). Unchanged guarantees: generated
content is DRAFT, claims verified (SymPy / source overlap / conflict check) and persisted, nothing becomes APPROVED automatically.

## Trust model
* Approval only via `POST /learning/assets/{id}/approve` (admin). Approving a revision marks the version it replaces SUPERSEDED.
* Readers see curriculum structure and APPROVED content only; coverage, quality, gaps, scans are editor-only; schedules/profiles/activation admin-only.
* Fetched web content stays UNVERIFIED; research results are never auto-published (`publication_requires_review`).

## Operations
* Jobs (`preparation_jobs`, `scan_runs`) are database rows with atomic claim, retries, dead-letter, stale recovery; Celery beat re-dispatches
  anything whose enqueue failed. Beat: preparation jobs 60 s, learning schedules 15 min, embeddings 10 min, plus the Stage 6 tasks.
* Migrations 0005/0006 are idempotent (0001 builds from current metadata) and reversible.
