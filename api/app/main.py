import asyncio
import json
import uuid
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from redis import asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.db import SessionLocal, engine, get_session
from common.models import Base, Job
from common.schemas import (
    JobCreateRequest,
    JobCreateResponse,
    JobStatusResponse,
    StageTiming,
    UploadRequest,
    UploadResponse,
)
from common.settings import settings
from common.storage import ensure_bucket, presigned_get_url, presigned_put_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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


@app.post("/v1/uploads", response_model=UploadResponse)
async def create_upload(req: UploadRequest) -> UploadResponse:
    object_key = f"uploads/{uuid.uuid4()}-{req.filename}"
    url = presigned_put_url(object_key, content_type=req.content_type)
    return UploadResponse(object_key=object_key, upload_url=url)


@app.post("/v1/jobs", response_model=JobCreateResponse, status_code=202)
async def create_job(
    req: JobCreateRequest, session: AsyncSession = Depends(get_session)
) -> JobCreateResponse:
    job = Job(input_object_key=req.object_key, params=req.params, status="pending")
    session.add(job)
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
        coarse_glb_url=presigned_get_url(job.coarse_glb_key) if job.coarse_glb_key else None,
        final_glb_url=presigned_get_url(job.final_glb_key) if job.final_glb_key else None,
        final_glb_compressed_url=(
            presigned_get_url(job.final_glb_compressed_key) if job.final_glb_compressed_key else None
        ),
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job(job_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> JobStatusResponse:
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
async def job_events(job_id: uuid.UUID):
    return StreamingResponse(_event_stream(job_id), media_type="text/event-stream")
