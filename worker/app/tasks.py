import json
import tempfile
import uuid
from pathlib import Path

from redis import asyncio as aioredis

from common.db import SessionLocal
from common.models import Job
from common.settings import settings
from common.storage import download_file, upload_file
from worker.app.pipeline import ComfyHttpPipeline, compress_glb

redis = aioredis.from_url(settings.redis_url, decode_responses=True)


async def _publish(job_id: uuid.UUID, payload: dict) -> None:
    await redis.publish(f"job:{job_id}:events", json.dumps(payload))


async def run_pipeline_job(ctx, job_id_str: str) -> None:
    job_id = uuid.UUID(job_id_str)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        job.status = "running"
        await session.commit()
        await _publish(job_id, {"status": "running"})

        image_ext = Path(job.input_object_key).suffix or ".png"
        with tempfile.NamedTemporaryFile(suffix=image_ext) as tmp:
            download_file(job.input_object_key, tmp.name)
            image_bytes = Path(tmp.name).read_bytes()

        async def on_stage(stage: str, seconds: float) -> None:
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
            job.coarse_glb_key = key
            await session.commit()
            await _publish(job_id, {"status": "running", "artifact": name})

        pipeline = ComfyHttpPipeline(on_stage=on_stage, on_artifact=on_artifact)

        try:
            artifacts = await pipeline.run(job_id, image_bytes, image_ext)

            final_key = f"artifacts/{job_id}/final.glb"
            final_compressed_key = f"artifacts/{job_id}/final.compressed.glb"

            upload_file(str(artifacts["final_path"]), final_key)

            with tempfile.NamedTemporaryFile(suffix=".glb") as compressed:
                compress_glb(artifacts["final_path"], Path(compressed.name))
                upload_file(compressed.name, final_compressed_key)

            job.final_glb_key = final_key
            job.final_glb_compressed_key = final_compressed_key
            job.status = "succeeded"
            await session.commit()
            await _publish(job_id, {"status": "succeeded"})

        except Exception as exc:  # noqa: BLE001 -- job failure must never crash the worker loop
            job.status = "failed"
            job.error = str(exc)
            await session.commit()
            await _publish(job_id, {"status": "failed", "error": str(exc)})
