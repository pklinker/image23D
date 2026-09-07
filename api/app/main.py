import asyncio
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
from common.storage import UPLOAD_PREFIX, ensure_bucket, object_exists, presigned_get_url, presigned_put_url

from .auth import require_admin_key, require_api_key
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
    allow_origins=settings.viewer_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/healthz")
async def healthz(session: AsyncSession = Depends(get_session)) -> dict:
    """Liveness/readiness for the compose healthcheck.

    Unauthenticated on purpose -- it reports only that this process is serving
    and can reach the database. Reaching it at all also means migrations
    completed, since the entrypoint runs `alembic upgrade head` before uvicorn
    starts, which is what lets the worker wait on the API rather than racing
    the schema.
    """
    await session.execute(select(1))
    return {"status": "ok"}


# --- API keys ---
# Single-tenant, API-keys-only per PLAN.md sec.4 for now (OIDC deferred until
# there's a real IdP tenant to point at). Key management is admin-scoped: a key
# issued to an integration can run jobs but cannot mint itself more keys or
# revoke anyone else's.


@app.post("/v1/api-keys", response_model=ApiKeyCreateResponse)
async def create_api_key(
    req: ApiKeyCreateRequest,
    session: AsyncSession = Depends(get_session),
    actor: ApiKey = Depends(require_admin_key),
) -> ApiKeyCreateResponse:
    plaintext = generate_api_key()
    api_key = ApiKey(name=req.name, scope=req.scope, key_hash=hash_api_key(plaintext))
    session.add(api_key)
    await session.flush()  # populates api_key.id (a Python-side default, assigned at flush)
    await log_action(
        session, actor.name, "apikey.create", "api_key", str(api_key.id),
        created_name=req.name, created_scope=req.scope,
    )
    await session.commit()
    return ApiKeyCreateResponse(id=api_key.id, name=api_key.name, scope=api_key.scope, key=plaintext)


@app.get("/v1/api-keys", response_model=list[ApiKeyInfo])
async def list_api_keys(session: AsyncSession = Depends(get_session), _: ApiKey = Depends(require_admin_key)):
    result = await session.execute(select(ApiKey).order_by(ApiKey.created_at))
    return list(result.scalars())


@app.post("/v1/api-keys/{key_id}/revoke")
async def revoke_api_key(
    key_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    actor: ApiKey = Depends(require_admin_key),
):
    # Refusing self-revocation keeps an admin from locking themselves -- and
    # possibly everyone -- out: minting the first key means writing straight to
    # Postgres, because no unauthenticated route can do it.
    if key_id == actor.id:
        raise HTTPException(400, "cannot revoke the key making the request")
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
    # Check the input up front. A key that does not resolve used to be accepted,
    # queued, and only discovered by the worker -- taking a GPU slot to fail.
    if not req.object_key.startswith(UPLOAD_PREFIX):
        raise HTTPException(400, f"object_key must start with {UPLOAD_PREFIX!r}")
    if not await asyncio.to_thread(object_exists, req.object_key):
        raise HTTPException(404, "uploaded object not found -- PUT to the upload_url first")

    # Store the *effective* params, defaults filled in, so the job record says
    # what actually ran rather than what the caller happened to mention.
    job = Job(
        input_object_key=req.object_key,
        params=req.params.model_dump(),
        status="pending",
        created_by=api_key.name,
    )
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
        params=job.params or {},
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
        coarse_glb_key=job.coarse_glb_key,
        final_glb_key=job.final_glb_key,
        final_glb_compressed_key=job.final_glb_compressed_key,
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


TERMINAL_STATUSES = ("succeeded", "failed")

# Idle streams get cut by proxies and load balancers. A comment frame keeps the
# connection alive without the client having to interpret anything.
SSE_HEARTBEAT_SECONDS = 15

# The confirmation is already in flight when subscribe() returns; this bound
# only exists so a misbehaving server cannot wedge the stream here.
SUBSCRIBE_CONFIRM_TIMEOUT = 5


async def _load_status(job_id: uuid.UUID) -> JobStatusResponse | None:
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        return _job_to_status(job) if job is not None else None


async def _event_stream(job_id: uuid.UUID):
    """Every frame is a complete JobStatusResponse.

    The worker's pubsub payloads are partial (`{"status": ..., "stage": ...}`)
    and were previously forwarded verbatim, so the first frame and the rest had
    different shapes. Re-reading the row on each notification costs one small
    query per stage transition and makes the stream self-describing -- and the
    worker commits before it publishes, so the row is never behind the event.
    """
    channel = f"job:{job_id}:events"
    pubsub = app.state.redis.pubsub()

    # Subscribe BEFORE the snapshot. The other order leaves a window in which a
    # notification lands after the row is read but before the subscription
    # exists; if the lost one is terminal, the stream never closes and the
    # browser's EventSource hangs until it gives up.
    await pubsub.subscribe(channel)
    try:
        # redis-py surfaces the subscription confirmation as a message. Left in
        # the queue, the first get_message() below returns None immediately
        # (having swallowed it) and the stream emits a heartbeat it never
        # actually waited for.
        await pubsub.get_message(timeout=SUBSCRIBE_CONFIRM_TIMEOUT)

        status = await _load_status(job_id)
        if status is None:
            return
        yield f"data: {status.model_dump_json()}\n\n"
        if status.status in TERMINAL_STATUSES:
            return

        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=SSE_HEARTBEAT_SECONDS
            )
            if message is None:
                yield ": keep-alive\n\n"
                continue

            status = await _load_status(job_id)
            if status is None:  # purged mid-stream
                return
            yield f"data: {status.model_dump_json()}\n\n"
            if status.status in TERMINAL_STATUSES:
                return
    finally:
        # aclose() as well as unsubscribe(): without it the pubsub's own
        # connection is never returned to the pool, so every stream that was
        # opened leaked one.
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


@app.get("/v1/jobs/{job_id}/events")
async def job_events(job_id: uuid.UUID, _: ApiKey = Depends(require_api_key)):
    # Checked here rather than inside the generator: raising HTTPException from
    # a generator body happens after the response headers are already on the
    # wire, which produces a broken stream rather than a 404.
    if await _load_status(job_id) is None:
        raise HTTPException(404, "job not found")

    return StreamingResponse(
        _event_stream(job_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Stops a reverse proxy such as nginx buffering the
            # stream and delivering it all at once at the end.
            "X-Accel-Buffering": "no",
        },
    )
