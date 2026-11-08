# Document Intelligence Upgrade — report

## 0. Execution status (read first)
**No test was executed.** You asked for the code to be written without running it, and the build sandbox had no network
(could not install pytest/FastAPI/SQLAlchemy/PyMuPDF; Tesseract had English only, no Arabic data). The only check that ran
was a Python `ast.parse` syntax pass over every `.py` file (0 errors). Everything below is **written, not verified**.
Run `pytest` before deploying; see section 9.

## 1. Architecture changes
```
API (Vercel)  ──validate/sign/enqueue──▶ Redis/Celery ──▶ Worker (Railway: Tesseract + ara/eng data)
                                                          process_ingestion
                                                           └ ingestion_pipeline.process_job
  claim(lease) → validate → download(private Supabase, size-capped) → sha256 verify → extract_document (router)
  → normalize → structure-aware chunking → save (document + chunks, one transaction) → embed (existing embedder)
```
- `app/services/docreader/` — pure extraction engine (no FastAPI/SQLAlchemy):
  `security` (size, filename, magic bytes, ZIP-bomb/zip-slip), `pdf` (PyMuPDF → pypdf fallback → per-page OCR, merge
  without duplication, tables via `find_tables`, header/footer removal in structured blocks only), `ocr`
  (Tesseract, language fallback, pixel/time limits, confidence), `arabic` (NFKC ligature folding, diacritics/tatweel,
  bidi controls, broken/reversed-extraction detection + repair, original always kept), `structure` (headings,
  questions, examples, equations, chemical formulas, captions, lists), `textual`, `office`, `images`, `router`.
- `structured_chunking.py` — chunks keep page range, section, chapter, language, method, confidence, hash; never cross
  a chapter boundary; tables split by rows with the header repeated.
- `ingestion_pipeline.py` — orchestration, atomic job claim with lease, stage tracking, error taxonomy
  (permanent → `FAILED`; transient → `RETRYING` → `DEAD_LETTER`), redaction, stale-job recovery, metrics.
- Original text is stored in `raw_documents.text_content` and `knowledge_chunks.content`; the normalized searchable copy
  is `knowledge_chunks.content_norm` (same folding rules as the existing retrieval query normalization).
- Idempotency: file SHA-256 (`raw_documents.file_sha256`) and normalized-text hash (existing unique `content_hash`);
  a concurrent duplicate is resolved via `IntegrityError` → duplicate result.
- Legacy behaviour preserved: `extract_segments`/`extract_text`/`store_upload` keep their contracts; legacy local-path
  uploads still work (path must now be inside `STORAGE_DIR`).

## 2. Modified files
`app/api/v1/documents.py`, `app/celery_app.py`, `app/core/config.py`, `app/models/knowledge.py`, `app/models/ops.py`,
`app/models/source.py`, `app/services/ingestion.py`, `app/services/storage.py`, `app/tasks.py`, `Dockerfile`,
`docker-compose.yml`, `.env.example`, `requirements.txt`, `requirements-dev.txt`.

## 3. New files
`app/services/docreader/{__init__,types,security,arabic,quality,structure,ocr,pdf,textual,office,images,router}.py`,
`app/services/structured_chunking.py`, `app/services/ingestion_pipeline.py`,
`migrations/versions/0007_document_intelligence.py`, `docs/RAILWAY_WORKER.md`,
`tests/docreader/{__init__,builders,test_arabic_and_structure,test_extraction,test_security,test_pipeline}.py`,
this file.

## 4. New dependencies
Python: `pymupdf`, `python-docx`, `openpyxl`, `python-pptx`, `pytesseract`, `Pillow`, `lxml`.
Dev: `reportlab`, `arabic-reshaper`, `python-bidi`.
System (Dockerfile): `tesseract-ocr`, `tesseract-ocr-ara`, `tesseract-ocr-eng`, `antiword`, `fonts-dejavu-core`;
optional `libreoffice-writer` via `--build-arg INSTALL_LIBREOFFICE=true` (better Arabic `.doc`).
Note: the original `requirements.txt` lists celery twice (pre-existing); left unchanged.

## 5. Database migration
`0007_document_intelligence` (additive, idempotent, guarded by schema inspection): `knowledge_chunks` +
`source_id, section_title, chapter_title, extraction_method, extraction_confidence`; `raw_documents` + `file_sha256`
(+index); `ingestion_jobs` + `stage, lease_expires_at`. No tables created, nothing removed.
Run: `alembic upgrade head`.

## 6. API changes (all existing endpoints and fields kept)
`GET /api/v1/documents/jobs/{id}` now also returns: `stage, max_attempts, document_id, file_type, extraction_method,
methods_used, total_pages, extracted_pages, ocr_pages, extracted_characters, chunks_created, failed_pages,
mean_confidence, language, duplicate, embedding, warnings, error_code, errors[]`. No storage URIs/tokens are exposed.
New: `POST /api/v1/documents/jobs/{id}/retry` (EDITOR; FAILED/DEAD_LETTER only) and
`GET /api/v1/documents/ingestion/metrics?days=30` (READER).
Accepted types: PDF, DOCX, DOC, TXT, MD, HTML, JSON, CSV, XLSX, PPTX, PNG, JPG/JPEG, TIFF.

## 7. Environment variables
Existing ones unchanged. New (all optional, defaults in `.env.example`): `INGESTION_TEMP_DIR`, `INGESTION_MAX_PAGES`,
`INGESTION_MAX_CHARS`, `INGESTION_SOFT_TIME_LIMIT_SECONDS`, `INGESTION_TIME_LIMIT_SECONDS`, `INGESTION_MAX_RETRIES`,
`INGESTION_LEASE_SECONDS`, `INGESTION_PENDING_REQUEUE_MINUTES`, `INGESTION_DOCUMENT_BUDGET_SECONDS`, `OCR_ENABLED`,
`OCR_LANGUAGES`, `OCR_DPI`, `OCR_MAX_PAGES_PER_DOCUMENT`, `OCR_PAGE_TIMEOUT_SECONDS`, `OCR_MAX_IMAGE_PIXELS`,
`PDF_MIN_PAGE_CHARS`, `PDF_MIN_PAGE_QUALITY`, `OFFICE_MAX_*`, `CHUNK_MAX_CHARS`, `CHUNK_OVERLAP_CHARS`, `EMBED_BATCH_SIZE`.
Keep `INGESTION_DOCUMENT_BUDGET_SECONDS` < `INGESTION_SOFT_TIME_LIMIT_SECONDS` < `INGESTION_TIME_LIMIT_SECONDS`.

## 8. Deployment
1. Push; set the new env vars (optional) on API and worker; API and worker must share `DATABASE_URL` and `REDIS_URL`.
2. Run `alembic upgrade head` once (before starting new workers).
3. Railway worker service: build from the Dockerfile; start command and sizing in `docs/RAILWAY_WORKER.md`.
   The build fails if `ara`/`eng` OCR data is missing.
4. Exactly one `beat` service (runs `recover_ingestion_jobs` every 5 min).
5. Vercel API: unchanged role — it never runs OCR.

## 9. Tests (written, NOT executed)
`pytest tests/docreader` covers: Arabic PDF, English PDF, scanned Arabic/English (fake OCR; real-Tesseract variants skip
when language data is missing), mixed PDF (OCR only on the scanned page), corrupted PDF, empty PDF, DOCX, XLSX, PPTX,
HTML, TXT (UTF-8 and cp1256), CSV/JSON/MD, images, duplicate processing, worker claim/lease/retry/dead-letter/recovery,
extraction fallback (pypdf), security (signature, MIME, ZIP bomb, zip-slip, filename), chunk provenance, metrics.
Also run the full existing suite (`pytest`) — model changes touch `knowledge_chunks`, `raw_documents`, `ingestion_jobs`.
Likely first-run friction: the Arabic-PDF test depends on glyph order produced by `arabic-reshaper`+`bidi` and on
PyMuPDF's extraction order, so its assertion may need adjusting.

## 10. Step-by-step test with the Ministry of Education PDF
Header for all calls: `X-API-Key: <EDITOR_API_KEY>`; `$API` = your API base URL.
1. `alembic upgrade head`; confirm the worker log shows the Celery worker on queues `ingestion,research,default`.
2. Sign: `curl -X POST $API/api/v1/documents/uploads/sign -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{"original_name":"moe_book.pdf","content_type":"application/pdf","file_size":<bytes>}'` → `object_path`, `upload_url`.
3. Upload: `curl -X PUT "<upload_url>" -H "Content-Type: application/pdf" --data-binary @moe_book.pdf`.
4. Complete: `curl -X POST $API/api/v1/documents/uploads/complete -H "X-API-Key: $KEY" -H "Content-Type: application/json" -d '{"object_path":"<object_path>","original_name":"moe_book.pdf","file_sha256":"<sha256sum of the file>"}'` → `job_id`.
5. Poll `GET $API/api/v1/documents/jobs/<job_id>` until `status` is `SUCCEEDED` (watch `stage`).
6. Check: `total_pages` equals the PDF page count; `extracted_pages` + `failed_pages` account for every page; `ocr_pages` is 0
   for a text PDF and > 0 for scanned pages; `chunks_created` > 0; `extraction_method` matches expectation; `errors` empty.
7. In PostgreSQL: `SELECT page_start,page_end,chapter_title,section_title,extraction_method,extraction_confidence,left(content,80) FROM knowledge_chunks WHERE document_id='<document_id>' ORDER BY chunk_index LIMIT 20;` — pages must match the printed book pages, Arabic must read in logical order.
8. Compare `raw_documents.text_content` (original) with `knowledge_chunks.content_norm` (normalized) for a few chunks.
9. Re-run steps 4–5 with the same file: expect `duplicate: true`, `duplicates_skipped: 1`, chunk count unchanged.
10. `GET $API/api/v1/documents/ingestion/metrics` — verify counts, OCR pages and Arabic quality.
11. Failure drill: kill the worker mid-job; within ~`INGESTION_LEASE_SECONDS` + 5 min the job is re-queued and finishes.
