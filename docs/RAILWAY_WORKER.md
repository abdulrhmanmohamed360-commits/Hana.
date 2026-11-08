# Railway worker deployment (document intelligence)

The repo has no Railway-specific file; Railway builds the existing `Dockerfile`, which now installs Tesseract with the
Arabic (`ara`) and English (`eng`) language data and `antiword` for legacy `.doc`. The image build **fails** if either
OCR language is missing, so a worker can never start without Arabic OCR.

## Services (all from this repo, same `Dockerfile`)

| Service | Start command |
|---|---|
| api (Vercel or Railway) | `uvicorn app.main:app --host 0.0.0.0 --port 8000` |
| worker | `celery -A app.tasks:celery_app worker --loglevel=INFO --queues=ingestion,research,default --concurrency=2 --max-tasks-per-child=25 --max-memory-per-child=1500000` |
| scheduler (exactly one) | `celery -A app.tasks:celery_app beat --loglevel=INFO` |

`beat` runs `recover_ingestion_jobs` every 5 minutes: it re-queues jobs whose worker crashed (expired lease) and jobs
that were never dispatched. Run only one beat instance.

## Required sizing
OCR of scanned pages is memory-hungry. Start with 2 GB RAM / 2 vCPU for the worker; lower `--concurrency` to 1 if you see
OOM kills. `OMP_THREAD_LIMIT=1` is already set in the image.

## Environment (identical on api and worker)
`DATABASE_URL`, `REDIS_URL`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_STORAGE_BUCKET`, `ADMIN_API_KEY`,
`EDITOR_API_KEY`, `READER_API_KEY`, optional `EMBEDDING_*` / `LLM_*`, plus the `INGESTION_*` / `OCR_*` variables listed in
`.env.example`. The API and the worker MUST point at the same `DATABASE_URL` and `REDIS_URL`.

## Do not
- Do not run OCR in Vercel request handlers (the API only validates, records the job and enqueues it).
- Do not put secrets in the Dockerfile or logs; error text stored on jobs is redacted of tokens and signed URLs.

## Optional: LibreOffice for `.doc`
Build with `--build-arg INSTALL_LIBREOFFICE=true` (Railway: set as a build variable) for the best Arabic fidelity on
legacy `.doc`. Without it the worker uses `antiword`.
