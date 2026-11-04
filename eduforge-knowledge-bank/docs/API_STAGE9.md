# Stage 9 API reference (`/api/v1`, header `X-API-Key`)
Roles: **R** reader · **E** editor · **A** admin. Errors use the project's standard JSON error body (404 not found, 409 duplicate/conflict, 422 invalid).

## Curriculum — `/curriculum`
| Method & path | Role | Purpose |
|---|---|---|
| `POST /curriculum` | E | create curriculum `{country, education_system, name, language, status}`; 409 on duplicate identity |
| `GET /curriculum?country=&status=` · `GET /curriculum/{id}` | R | list / read |
| `PATCH /curriculum/{id}` | E | description, language, meta |
| `GET /curriculum/{id}/tree?max_depth=` | R | full hierarchy with objectives |
| `POST /curriculum/{id}/import` | E | duplicate-safe tree import; `prerequisites:[lesson names]` become PREREQUISITE_OF edges |
| `POST /curriculum/{id}/new-version` · `POST /curriculum/{id}/activate` | E · A | versioning; activation archives the old version and carries content over |
| `POST /curriculum/nodes` · `GET/PATCH/DELETE /curriculum/nodes/{id}` | E (GET: R) | node CRUD; placement and duplicates validated; delete blocked while children exist |
| `GET /curriculum/nodes/{id}/context` | R | objectives, prerequisite chain, missing prerequisites, next lessons, topics/concepts |
| `POST /curriculum/resolve` | R | `{country, grade, subject, lesson, term?, unit?, curriculum_id?}` → RESOLVED / AMBIGUOUS / NOT_FOUND |
| `POST /curriculum/link-assets` | E | backfill: map pre-Stage-9 assets onto lessons (unambiguous matches only) |

## Coverage — `/coverage` (editor+: exposes draft/pending/unverified state)
`GET /coverage/report?country&grade&term&subject&unit&curriculum_id&education_system&details` → `totals` {total_subjects, total_lessons, covered_lessons, missing_lessons,
incomplete_lessons, pending_review_lessons, outdated_lessons, unverified_claims, pending_reviews, outdated_knowledge, concepts_without_explanation,
formulas_without_examples, exercises_without_answers, assets_needing_sources, coverage_percentage}, `by_subject/by_grade/by_unit`, `lessons[]`.
`GET /coverage/lessons/{node_id}` · `GET /coverage/profiles` (E) · `PUT/DELETE /coverage/profiles/{key}` (A) customise completeness criteria
(`requirements:[{asset_type,min_count,min_chars,require_sources,require_answers}]`, `subject_pattern`, `freshness_days`).
Lesson status: MISSING · INCOMPLETE · PENDING_REVIEW · OUTDATED · COMPLETE (only APPROVED content counts).

## Knowledge graph — `/graph`
`POST /graph/edges` (E) `{relation, from_type, from_id, to_type, to_id, confidence, provenance}` · `GET /graph/edges/{id}` (R, filtered) · `PATCH` / `DELETE ?reason=` (E, audited) ·
`GET /graph/entities/{id}/edges?relation&direction` (R: only structure + APPROVED content; E: everything) · `GET /graph/nodes/{id}/related?depth` (R) ·
`GET /graph/nodes/{id}/prerequisites` (R, ordered learning path) · `GET /graph/nodes/{id}/missing-prerequisites` (E) · `POST /graph/link-asset` (E).
Entity types: NODE, ASSET, CLAIM, SOURCE. Relations: PREREQUISITE_OF, PART_OF, RELATED_TO, EXPLAINS, USES_FORMULA, HAS_EXAMPLE, HAS_QUESTION, SUPPORTED_BY, CONTRADICTS, EXTENDS.

## Quality — `/quality` (editor+)
`GET /quality/assets/{id}?persist=` · `GET /quality/lessons/{node_id}` · `GET /quality/assets/{id}/history` · `POST /quality/assets/{id}/revisions` (new DRAFT version; approve via the existing admin endpoint).
Response: `support_score`, `correctness` (DETERMINISTICALLY_VERIFIED | REJECTED | CONFLICTED | UNKNOWN) + `correctness_basis`, `flags`, `needs_human_review`, and per-dimension `{score, reason}`
for verification, source_reliability, evidence_strength, consistency, completeness, freshness, review.

## Continuous learning — `/continuous-learning` (editor+, schedules admin)
`GET /gaps?status&gap_type&node_id` · `GET/PATCH /gaps/{id}` (dismiss/resolve with note) · `POST /gaps/{id}/prepare` (202; queues a preparation job → DRAFT content for review) ·
`POST /scans` (manual, optional scope) · `GET /scans`, `GET /scans/{id}` (history, stats, log) · `POST /scans/{id}/cancel` · `POST /scans/{id}/retry` ·
`GET/POST /schedules`, `PATCH/DELETE /schedules/{id}` (A). Gap types: MISSING_LESSON, INCOMPLETE_CONTENT, NEEDS_REVIEW, OUTDATED_CONTENT, UNVERIFIED_CLAIMS, CONCEPT_WITHOUT_EXPLANATION,
FORMULA_WITHOUT_EXAMPLE, EXERCISE_WITHOUT_ANSWER, NEEDS_SOURCES, MISSING_PREREQUISITE, SOURCE_CHANGED, NEW_CONFLICT.
Limits: `LEARNING_MAX_RESEARCH_PER_SCAN`, `LEARNING_MAX_RESEARCH_PER_DAY`, `LEARNING_MAX_GAPS_PER_SCAN`; external research needs `TAVILY_API_KEY`, otherwise gaps stay OPEN and the scan log says so.

## Prepare (Stage 8, extended) — `POST /knowledge/prepare`
New optional request field `curriculum_id`; new response fields `curriculum` (resolution status, path, objectives, prerequisites, missing prerequisites) and `knowledge_state` (lesson coverage).
