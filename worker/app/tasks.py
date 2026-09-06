import asyncio
import json
import logging
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from redis import asyncio as aioredis
from sqlalchemy import select

from common.audit import log_action
from common.db import SessionLocal
from common.models import Job
from common.settings import settings
from common.storage import delete_object, download_file, upload_file
from worker.app.embedded_pipeline import ComfyEmbeddedPipeline
from worker.app.pipeline import ComfyHttpPipeline, compress_glb

PIPELINES = {"embedded": ComfyEmbeddedPipeline, "http": ComfyHttpPipeline}

# Job.error is unbounded Text, but an error also gets published to every SSE
# listener and rendered in the browser -- a full ComfyUI status dump helps
# nobody there.
MAX_ERROR_CHARS = 2000

logger = logging.getLogger(__name__)

redis = aioredis.from_url(settings.redis_url, decode_responses=True)


async def _publish(job_id: uuid.UUID, payload: dict) -> None:
    await redis.publish(f"job:{job_id}:events", json.dumps(payload))


async def _update_job(job_id: uuid.UUID, **fields) -> None:
    """Apply field updates inside a short-lived session of its own.

    Deliberately not one session shared across the whole ~70s job: a failure
    write must not depend on the state of a session that was already
    mid-transaction when the failure landed, and holding a Postgres connection
    open for the duration of a GPU job buys nothing.
    """
    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        for name, value in fields.items():
            setattr(job, name, value)
        await session.commit()


async def _fail(job_id: uuid.UUID, error: str) -> None:
    error = error[:MAX_ERROR_CHARS]
    await _update_job(job_id, status="failed", error=error)
    await _publish(job_id, {"status": "failed", "error": error})
    _release_gpu_memory()


def _release_gpu_memory() -> None:
    """Free cached CUDA blocks after a failed run.

    A run that died partway through leaves allocator blocks behind; the next
    job peaks at ~12.9GB of 16GB, so that fragmentation is enough to turn a
    working job into an OOM. No-op when torch/ComfyUI isn't importable (the
    http backend can run in a CPU-only container).
    """
    try:
        import comfy.model_management as model_management
    except ImportError:
        return
    try:
        model_management.soft_empty_cache()
    except Exception:  # noqa: BLE001 -- best-effort cleanup, never mask the real error
        logger.warning("soft_empty_cache() failed during cleanup", exc_info=True)


def _download_input(object_key: str, suffix: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        download_file(object_key, tmp.name)
        return Path(tmp.name).read_bytes()


async def run_pipeline_job(ctx, job_id_str: str) -> None:
    job_id = uuid.UUID(job_id_str)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        input_object_key = job.input_object_key

    image_ext = Path(input_object_key).suffix or ".png"

    await _update_job(job_id, status="running", error=None)
    await _publish(job_id, {"status": "running"})

    async def on_stage(stage: str, seconds: float) -> None:
        async with SessionLocal() as session:
            job = await session.get(Job, job_id)
            if job is None:
                return
            job.stage = stage
            job.stage_timings = [*job.stage_timings, {"stage": stage, "seconds": round(seconds, 2)}]
            await session.commit()
        await _publish(job_id, {"status": "running", "stage": stage})

    async def on_artifact(name: str, path: Path) -> None:
        # Fired as soon as the coarse mesh exists (PLAN.md sec.6), well before
        # the rest of the pipeline finishes -- lets the viewer swap it in
        # instead of showing nothing for the full ~60s run.
        key = f"artifacts/{job_id}/{name}.glb"
        upload_file(str(path), key)
        await _update_job(job_id, coarse_glb_key=key)
        await _publish(job_id, {"status": "running", "artifact": name})

    pipeline = PIPELINES[settings.pipeline_backend](on_stage=on_stage, on_artifact=on_artifact)

    # Everything that can fail lives inside this try. The input download used to
    # sit outside it, so a bad or missing object key raised past the handler and
    # left the row stuck at "running" forever -- invisible to retention, which
    # only touches terminal jobs, and an SSE stream that never closes.
    try:
        image_bytes = _download_input(input_object_key, image_ext)
        artifacts = await pipeline.run(job_id, image_bytes, image_ext)

        final_key = f"artifacts/{job_id}/final.glb"
        final_compressed_key = f"artifacts/{job_id}/final.compressed.glb"

        upload_file(str(artifacts["final_path"]), final_key)

        with tempfile.NamedTemporaryFile(suffix=".glb") as compressed:
            compress_glb(artifacts["final_path"], Path(compressed.name))
            upload_file(compressed.name, final_compressed_key)

        await _update_job(
            job_id,
            final_glb_key=final_key,
            final_glb_compressed_key=final_compressed_key,
            status="succeeded",
        )
        await _publish(job_id, {"status": "succeeded"})

    except asyncio.CancelledError:
        # ARQ raises this on worker shutdown or an explicit abort. Note the
        # ordinary timeout path does NOT come through here: the pipeline
        # enforces its own deadline (settings.pipeline_timeout_seconds) so the
        # failure can be recorded without doing database work from inside a
        # cancellation handler. Re-raised so ARQ can finish its own bookkeeping.
        logger.warning("job %s cancelled", job_id)
        await _fail(job_id, "cancelled (worker shutdown or abort)")
        raise

    except Exception as exc:  # noqa: BLE001 -- job failure must never crash the worker loop
        logger.exception("job %s failed", job_id)
        await _fail(job_id, f"{type(exc).__name__}: {exc}")


async def fail_orphaned_jobs() -> None:
    """Mark jobs left in `running` by a dead worker as failed, at startup.

    With max_jobs=1 and a single worker, any job still `running` when the worker
    boots was owned by a process that no longer exists -- nothing else could be
    working on it. Left alone the row stays `running` forever: retention
    deliberately skips non-terminal jobs, and any SSE client is still waiting on
    it. Revisit if a second worker is ever added (PLAN-VALOR.md F3) -- that needs
    a worker id on the row to tell "orphaned" from "running elsewhere".
    """
    error = "worker restarted while this job was running"

    async with SessionLocal() as session:
        result = await session.execute(select(Job).where(Job.status == "running"))
        jobs = list(result.scalars())
        for job in jobs:
            job.status = "failed"
            job.error = error
            await log_action(session, "system:worker", "job.orphaned", "job", str(job.id))
        await session.commit()

    for job in jobs:
        logger.warning("marked orphaned job %s as failed", job.id)
        await _publish(job.id, {"status": "failed", "error": error})


async def purge_old_jobs(ctx) -> None:
    """PLAN.md sec.4 retention policy. Runs daily (see worker_settings.py's
    cron_jobs). Only touches terminal jobs -- a stuck pending/running job is
    a bug to investigate, not something retention should silently clean up."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.retention_days)

    async with SessionLocal() as session:
        result = await session.execute(
            select(Job).where(Job.status.in_(["succeeded", "failed"]), Job.updated_at < cutoff)
        )
        jobs = list(result.scalars())

        for job in jobs:
            for key in (job.input_object_key, job.coarse_glb_key, job.final_glb_key, job.final_glb_compressed_key):
                if key:
                    delete_object(key)
            await log_action(
                session, "system:retention", "job.purge", "job", str(job.id),
                age_days=(datetime.now(timezone.utc) - job.updated_at).days,
            )
            await session.delete(job)

        await session.commit()
