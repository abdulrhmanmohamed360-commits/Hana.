# Knowledge preparation API (Stage 8)

All endpoints are under `/api/v1/knowledge`, authenticated with `X-API-Key`. `prepare` and `jobs` need the **editor**
role because preparing a lesson stores DRAFT assets and verification records. Generated content is never approved by
these endpoints; approval stays `POST /api/v1/learning/assets/{id}/approve` (admin).

## POST /prepare
Request (`PrepareRequest`): `country`, `curriculum`, `grade`, `semester`, `subject`, `unit?`, `lesson`,
`requested_content` (any of `explanation, definitions, formulas, solved_examples, exercises, questions, answers, summary,
sources`; default = all but `formulas`), `difficulty` (`beginner|intermediate|advanced`), `language`,
`mode` (`sync` default | `async`), `allow_generation` (default true), `allow_research` (async only).

| mode | what runs | response |
|---|---|---|
| `sync` | approved-asset lookup, hybrid retrieval, reuse of earlier drafts, deterministic generation, verification. No LLM, no external research (at most one query-embedding call). | `200` + `PreparePackage` |
| `async` | the above + Research/Analysis/Teacher/Verification agents (needs `LLM_API_KEY`) + optional external research | `202` + `{job_id, status, status_url, deduplicated}` |

### Workflow
1. Approved `LearningAsset`s matching country/grade/subject/lesson/language (Arabic-normalized comparison) → `trust: APPROVED`.
2. Hybrid retrieval of source passages (lexical + PostgreSQL FTS + pgvector, fused with RRF).
3. Earlier DRAFT assets for the same request are reused (verdicts restored from stored claims), never duplicated.
4. Missing types are generated: linear-equation examples/exercises/MCQs by the deterministic generator; text by agents (async) or
   extractively from retrieved passages (explanation/definitions only, verbatim, with page provenance).
5. Every generated item is stored as a DRAFT asset, its claims are extracted, verified and persisted (`extracted_claims`,
   `evidence_records`, `verification_jobs`, `audit_logs`). Items with a REJECTED claim are removed from the package and listed under `rejected`.
6. `answers` and `sources` are derived views, not stored assets.

### Statuses (per item and per claim)
`APPROVED` human-approved asset · `MATH_VERIFIED` deterministic SymPy check passed · `SOURCE_SUPPORTED` ≥60 % lexical support from a
reviewed source and no conflict (not proof) · `UNVERIFIED` could not be checked / only unreviewed web content · `CONFLICTED` a source
passage contradicts it · `REJECTED` failed a deterministic check. An item takes the **worst** status of its claims.
`trust` is `APPROVED`, `DRAFT` or `SOURCE_MATERIAL`; `contains_unapproved_content` is true if any item is not `APPROVED`.

### Package fields
`status` (`COMPLETE|PARTIAL|NOT_READY`), `items{type:[PackageItem]}`, `missing[{content_type, reason}]`, `rejected`,
`conflicts` (cross-source disagreements in the retrieved evidence), `trust_summary`, `retrieval{mode, signals_used,
signals_unavailable}`, `workflow` (trace), `advisory_notes` (agent review, advisory only), `notice`.

## GET /jobs/{job_id}  ·  GET /jobs?status=
`JobStatus`: `status` (`PENDING|RUNNING|RETRYING|SUCCEEDED|DEAD_LETTER`), `progress{stage, percent, note}`
(stages: queued → retrieving → planning → researching → generating → verifying → assembling → done), `attempts`,
`last_error`, and `result` (a `PreparePackage`) when finished. Jobs live in `preparation_jobs`, so they survive restarts.
Identical active requests are deduplicated. Failed jobs retry (default 3 attempts) and then dead-letter; a Celery beat task
re-dispatches PENDING/RETRYING jobs (e.g. if the broker was down at enqueue time) and recovers jobs stuck in RUNNING > 30 min.

## Research task status
`GET /api/v1/growth/tasks/{id}` (editor) — status of a single research task. Fetched pages are always `UNVERIFIED`.

## POST /retrieve (reader)
Hybrid retrieval; response adds `signals_used`, `signals_unavailable`, per-hit `signals` (rank per signal). `retrieval_mode`
is `lexical_baseline` when only one signal ran, `hybrid_rrf` otherwise. Never generates an answer.

## POST /embeddings/backfill?batch=64 (editor)
Embeds chunks lacking vectors (also runs every 10 min via beat when a provider is configured). 503 if no provider.

## Real generator output (executed, seed "demo", beginner, Arabic)
```
مثال 1 — حل المعادلة: 8س - 9 = 15
1. نضيف 9 إلى الطرفين: 8س = 24
2. نقسم الطرفين على 8: س = 3        الإجابة: س = 3
حل المعادلة التالية: 6س + 8 = -40   الإجابة النموذجية: س = -8
ما قيمة س التي تحقق المعادلة: 6س + 13 = 49 ؟   أ) 6  ب) 5  ج) 12  د) 0   (الصحيح: أ)
```
All of these returned `MATH_VERIFIED` from the independent SymPy verifier when run. A captured *HTTP* response from a running
server is not included because the API could not be started in the authoring environment.
