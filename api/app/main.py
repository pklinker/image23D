import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from redis import asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.audit import log_action
from common.db import SessionLocal, get_session
from common.models import ApiKey, Job
from common.schemas import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyInfo,
    JobCreateRequest,
    JobCreateResponse,
    JobStatusResponse,
    StageTiming,
    UploadRequest,
    UploadResponse,
)
from common.security import generate_api_key, hash_api_key
from common.settings import settings
from common.storage import ensure_bucket, presigned_get_url, presigned_put_url

from .auth import require_api_key
from .rate_limit import require_job_creation_rate_limit, require_upload_rate_limit


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is managed by Alembic migrations (see alembic/), run at
    # container startup -- not by create_all(), which only creates tables
    # that don't exist yet and silently no-ops on altered columns on
    # existing ones (that gap is exactly how Phase 4 started: adding
    # Job.created_by to the already-existing jobs table did nothing until
    # migrations were added).
    ensure_bucket()
    app.state.redis_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    yield
    await app.state.redis.close()


app = FastAPI(title="image23D", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.viewer_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- API keys ---
# Single-tenant, API-keys-only per PLAN.md sec.4 for now (OIDC deferred until
# there's a real IdP tenant to point at) -- any valid key can manage other
# keys. There's no admin/service scope split yet; add one before this is
# exposed beyond a small trusted internal team.


@app.post("/v1/api-keys", response_model=ApiKeyCreateResponse)
async def create_api_key(
    req: ApiKeyCreateRequest,
    session: AsyncSession = Depends(get_session),
    actor: ApiKey = Depends(require_api_key),
) -> ApiKeyCreateResponse:
    plaintext = generate_api_key()
    api_key = ApiKey(name=req.name, key_hash=hash_api_key(plaintext))
    session.add(api_key)
    await session.flush()  # populates api_key.id (a Python-side default, assigned at flush)
    await log_action(session, actor.name, "apikey.create", "api_key", str(api_key.id), created_name=req.name)
    await session.commit()
    return ApiKeyCreateResponse(id=api_key.id, name=api_key.name, key=plaintext)


@app.get("/v1/api-keys", response_model=list[ApiKeyInfo])
async def list_api_keys(session: AsyncSession = Depends(get_session), _: ApiKey = Depends(require_api_key)):
    result = await session.execute(select(ApiKey).order_by(ApiKey.created_at))
    return list(result.scalars())


@app.post("/v1/api-keys/{key_id}/revoke")
async def revoke_api_key(
    key_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: ApiKey = Depends(require_api_key),
):
    api_key = await session.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(404, "api key not found")
    api_key.revoked_at = datetime.now(timezone.utc)
    await log_action(session, actor.name, "apikey.revoke", "api_key", str(key_id))
    await session.commit()
    return {"revoked": True}


# --- Uploads & jobs ---


@app.post("/v1/uploads", response_model=UploadResponse)
async def create_upload(req: UploadRequest, _: ApiKey = Depends(require_upload_rate_limit)) -> UploadResponse:
    object_key = f"uploads/{uuid.uuid4()}-{req.filename}"
    url = presigned_put_url(object_key, content_type=req.content_type)
    return UploadResponse(object_key=object_key, upload_url=url)


@app.post("/v1/jobs", response_model=JobCreateResponse, status_code=202)
async def create_job(
    req: JobCreateRequest,
    session: AsyncSession = Depends(get_session),
    api_key: ApiKey = Depends(require_job_creation_rate_limit),
) -> JobCreateResponse:
    job = Job(input_object_key=req.object_key, params=req.params, status="pending", created_by=api_key.name)
    session.add(job)
    await session.flush()  # populates job.id (a Python-side default, assigned at flush)
    await log_action(session, api_key.name, "job.create", "job", str(job.id), object_key=req.object_key)
    await session.commit()
    await app.state.redis_pool.enqueue_job("run_pipeline_job", str(job.id))
    return JobCreateResponse(job_id=job.id)


def _job_to_status(job: Job) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        stage=job.stage,
        error=job.error,
        stage_timings=[StageTiming(**t) for t in job.stage_timings],
        total_seconds=job.total_seconds,
        gpu_peak_mb=job.gpu_peak_mb,
        coarse_glb_url=presigned_get_url(job.coarse_glb_key) if job.coarse_glb_key else None,
        final_glb_url=presigned_get_url(job.final_glb_key) if job.final_glb_key else None,
        final_glb_compressed_url=(
            presigned_get_url(job.final_glb_compressed_key) if job.final_glb_compressed_key else None
        ),
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(
    job_id: uuid.UUID, session: AsyncSession = Depends(get_session), _: ApiKey = Depends(require_api_key)
) -> JobStatusResponse:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return _job_to_status(job)


async def _event_stream(job_id: uuid.UUID):
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        yield f"data: {_job_to_status(job).model_dump_json()}\n\n"
        if job.status in ("succeeded", "failed"):
            return

    pubsub = app.state.redis.pubsub()
    await pubsub.subscribe(f"job:{job_id}:events")
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            payload = json.loads(message["data"])
            yield f"data: {json.dumps(payload)}\n\n"
            if payload.get("status") in ("succeeded", "failed"):
                break
    finally:
        await pubsub.unsubscribe(f"job:{job_id}:events")


@app.get("/v1/jobs/{job_id}/events")
async def job_events(job_id: uuid.UUID, _: ApiKey = Depends(require_api_key)):
    return StreamingResponse(_event_stream(job_id), media_type="text/event-stream")
