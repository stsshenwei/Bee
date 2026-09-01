# Async Runtime Runbook

Bee's Weknora-style async runtime uses Redis for task delivery and PostgreSQL for authoritative state.

## Local Redis

```powershell
docker run --name bee-redis -p 6379:6379 -d redis:latest
```

## Backend Env

```env
ASYNC_RUNTIME_ENABLED=true
ASYNC_RUNTIME_MODE=celery
ASYNC_RUNTIME_BROKER_URL=redis://localhost:6379/0
ASYNC_RUNTIME_RESULT_BACKEND_URL=
PROCESSING_WORKER_ENABLED=true
ASYNC_RUNTIME_LOCAL_WORKER_ENABLED=false
```

## Worker Pools

Run from `backend/` after installing requirements.

Windows local development:

```powershell
.\.venv\Scripts\celery.exe -A app.workers.celery_app:celery_app worker -Q core --pool=solo --concurrency=1 --loglevel=info
.\.venv\Scripts\celery.exe -A app.workers.celery_app:celery_app worker -Q wiki --pool=solo --concurrency=1 --loglevel=info
.\.venv\Scripts\celery.exe -A app.workers.celery_app:celery_app worker -Q postprocess,enrichment,maintenance,shared --pool=solo --concurrency=1 --loglevel=info
```

Production-style Linux commands can use configured concurrency:

```bash
celery -A app.workers.celery_app:celery_app worker -Q core --concurrency=8 --loglevel=info
celery -A app.workers.celery_app:celery_app worker -Q wiki --concurrency=8 --loglevel=info
celery -A app.workers.celery_app:celery_app worker -Q postprocess --concurrency=2 --loglevel=info
celery -A app.workers.celery_app:celery_app worker -Q enrichment --concurrency=12 --loglevel=info
celery -A app.workers.celery_app:celery_app worker -Q maintenance --concurrency=4 --loglevel=info
celery -A app.workers.celery_app:celery_app worker -Q shared --concurrency=6 --loglevel=info
```

## Checks

- `GET /health` includes `async_runtime.mode`, `queues`, `concurrency`, and `routes`.
- Upload confirmation should create `document_processing_task` rows before worker completion.
- In Celery mode, API logs should show `async_runtime.queue.dispatched`.
- If a worker is idle, compare its `-Q` queue list with `/health.async_runtime.routes`.

## Fallback

Set `ASYNC_RUNTIME_ENABLED=false` or `ASYNC_RUNTIME_MODE=local` to return to the PostgreSQL/local worker loop. Keep `PROCESSING_WORKER_ENABLED=true` when Wiki ingest should continue in local mode.
