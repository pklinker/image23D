"""Job state machine tests -- PLAN-BUGFIX.md item 1.

Each test names the failure it locks down. All of them fail against the code as
it was before item 1: a job that raised outside the try block, or was cancelled,
stayed `running` forever.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from common.db import SessionLocal
from common.settings import settings
from common.models import AuditLog, Job
from worker.app import tasks


async def _make_job(**overrides) -> uuid.UUID:
    async with SessionLocal() as session:
        job = Job(
            input_object_key=overrides.pop("input_object_key", "uploads/test.png"),
            params={},
            status=overrides.pop("status", "pending"),
            stage_timings=[],
            **overrides,
        )
        session.add(job)
        await session.commit()
        return job.id


async def _get_job(job_id: uuid.UUID) -> Job:
    async with SessionLocal() as session:
        return await session.get(Job, job_id)


class StubPipeline:
    """Stands in for ComfyEmbeddedPipeline: same constructor and run() shape."""

    def __init__(self, on_stage=None, on_artifact=None, behaviour=None):
        self.on_stage = on_stage
        self.on_artifact = on_artifact
        self.behaviour = behaviour
        self.started = asyncio.Event()

    async def run(self, job_id, image_bytes, image_ext) -> dict:
        self.started.set()
        return await self.behaviour(self, job_id)


@pytest.fixture
def stub_pipeline(monkeypatch, tmp_path):
    """Installs a stub pipeline and neutralises S3/compression side effects."""
    holder = {}

    def install(behaviour):
        def factory(on_stage=None, on_artifact=None):
            holder["pipeline"] = StubPipeline(on_stage, on_artifact, behaviour)
            return holder["pipeline"]

        monkeypatch.setitem(tasks.PIPELINES, "embedded", factory)
        monkeypatch.setitem(tasks.PIPELINES, "http", factory)
        return holder

    # Patched at the storage boundary (not at an internal helper) so these
    # tests run unchanged against the pre-fix code too.
    monkeypatch.setattr(tasks, "download_file", lambda key, path: None)
    monkeypatch.setattr(tasks, "upload_file", lambda local, key: None)
    monkeypatch.setattr(tasks, "compress_glb", lambda src, dst: dst.write_bytes(b"glb"))
    return install


async def test_success_path_records_artifacts(stub_pipeline, tmp_path):
    final = tmp_path / "final.glb"
    final.write_bytes(b"glb")

    async def behaviour(pipeline, job_id):
        await pipeline.on_stage("shape_upsample", [{"stage": "shape_upsample", "seconds": 1.5}], 1.5)
        return {"final_path": final, "gpu_peak_mb": 11234}

    stub_pipeline(behaviour)
    job_id = await _make_job()

    await tasks.run_pipeline_job(None, str(job_id))

    job = await _get_job(job_id)
    assert job.status == "succeeded"
    assert job.final_glb_key == f"artifacts/{job_id}/final.glb"
    assert job.final_glb_compressed_key == f"artifacts/{job_id}/final.compressed.glb"
    assert job.stage_timings == [{"stage": "shape_upsample", "seconds": 1.5}]
    assert job.total_seconds == 1.5
    assert job.gpu_peak_mb == 11234


async def test_download_failure_marks_job_failed(stub_pipeline, monkeypatch):
    """The headline bug: status=running was committed before the try block, so a
    bad object key raised past the handler and left the row `running` forever --
    invisible to retention, with the SSE stream never closing."""

    async def behaviour(pipeline, job_id):  # pragma: no cover -- must never run
        raise AssertionError("pipeline should not start when the download fails")

    stub_pipeline(behaviour)

    def boom(key, path):
        raise FileNotFoundError(f"no such object: {key}")

    monkeypatch.setattr(tasks, "download_file", boom)
    job_id = await _make_job(input_object_key="uploads/does-not-exist.png")

    await tasks.run_pipeline_job(None, str(job_id))  # must not raise

    job = await _get_job(job_id)
    assert job.status == "failed"
    assert "does-not-exist.png" in job.error
    assert job.error.startswith("FileNotFoundError")


async def test_pipeline_exception_marks_job_failed(stub_pipeline):
    async def behaviour(pipeline, job_id):
        raise RuntimeError("CUDA out of memory")

    stub_pipeline(behaviour)
    job_id = await _make_job()

    await tasks.run_pipeline_job(None, str(job_id))

    job = await _get_job(job_id)
    assert job.status == "failed"
    assert job.error == "RuntimeError: CUDA out of memory"


async def test_cancellation_marks_job_failed_and_reraises(stub_pipeline):
    """Cancellation is what ARQ raises at job_timeout and on worker shutdown.

    The old handler caught only `Exception`, so a cancelled job stayed `running`.
    This also proves the failure write actually completes from inside a
    CancelledError handler -- the assumption the fix rests on -- by cancelling a
    real task rather than raising CancelledError synthetically.
    """
    async def behaviour(pipeline, job_id):
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled")

    holder = stub_pipeline(behaviour)
    job_id = await _make_job()

    task = asyncio.create_task(tasks.run_pipeline_job(None, str(job_id)))

    async def _wait_until_running():
        while "pipeline" not in holder:
            await asyncio.sleep(0.01)
        await holder["pipeline"].started.wait()

    # Cancel only once the pipeline is genuinely in flight -- cancelling earlier
    # would test a different (and easier) code path.
    await asyncio.wait_for(_wait_until_running(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    job = await _get_job(job_id)
    assert job.status == "failed"
    assert "cancelled" in job.error


async def test_missing_job_is_a_no_op(stub_pipeline):
    async def behaviour(pipeline, job_id):  # pragma: no cover
        raise AssertionError("pipeline should not start for an unknown job")

    stub_pipeline(behaviour)
    await tasks.run_pipeline_job(None, str(uuid.uuid4()))


async def test_fail_orphaned_jobs_recovers_stuck_rows():
    """A worker that dies mid-job leaves `running` rows nothing will ever finish."""
    stuck = await _make_job(status="running")
    pending = await _make_job(status="pending")
    done = await _make_job(status="succeeded")

    await tasks.fail_orphaned_jobs()

    assert (await _get_job(stuck)).status == "failed"
    assert "worker restarted" in (await _get_job(stuck)).error
    # Only `running` is orphaned: a pending job is still queued, and a terminal
    # job is finished.
    assert (await _get_job(pending)).status == "pending"
    assert (await _get_job(done)).status == "succeeded"

    async with SessionLocal() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.action == "job.orphaned"))
        entries = list(result.scalars())
    assert [e.resource_id for e in entries] == [str(stuck)]
    assert entries[0].actor == "system:worker"


async def test_error_is_truncated_before_publishing(stub_pipeline):
    async def behaviour(pipeline, job_id):
        raise RuntimeError("x" * 10_000)

    stub_pipeline(behaviour)
    job_id = await _make_job()

    await tasks.run_pipeline_job(None, str(job_id))

    job = await _get_job(job_id)
    assert len(job.error) == tasks.MAX_ERROR_CHARS


# --- item 6: scratch-file cleanup runs on every exit path -----------------


@pytest.fixture
def comfy_dirs(tmp_path, monkeypatch):
    inp = tmp_path / "comfy-input"
    out = tmp_path / "comfy-output"
    inp.mkdir()
    out.mkdir()
    monkeypatch.setattr(settings, "comfy_shared_input_dir", str(inp))
    monkeypatch.setattr(settings, "comfy_shared_output_dir", str(out))
    return inp, out


def _leave_scratch_files(inp, out, job_id):
    """What a real run leaves in the shared volumes."""
    from worker.app.pipeline import job_input_filename, job_output_subdir

    (inp / job_input_filename(job_id, ".png")).write_bytes(b"png")
    job_out = out / job_output_subdir(job_id)
    job_out.mkdir(parents=True)
    (job_out / "final_00001_.glb").write_bytes(b"glb")
    return inp / job_input_filename(job_id, ".png"), job_out


async def test_scratch_files_removed_after_success(stub_pipeline, comfy_dirs, tmp_path):
    inp, out = comfy_dirs
    final = tmp_path / "final.glb"
    final.write_bytes(b"glb")
    paths = {}

    async def behaviour(pipeline, job_id):
        paths["files"] = _leave_scratch_files(inp, out, job_id)
        return {"final_path": final}

    stub_pipeline(behaviour)
    job_id = await _make_job()

    await tasks.run_pipeline_job(None, str(job_id))

    input_file, job_out = paths["files"]
    assert (await _get_job(job_id)).status == "succeeded"
    assert not input_file.exists()
    assert not job_out.exists()


async def test_scratch_files_removed_after_failure(stub_pipeline, comfy_dirs):
    """The leak was worst here: a failed run's files were never collected."""
    inp, out = comfy_dirs
    paths = {}

    async def behaviour(pipeline, job_id):
        paths["files"] = _leave_scratch_files(inp, out, job_id)
        raise RuntimeError("CUDA out of memory")

    stub_pipeline(behaviour)
    job_id = await _make_job()

    await tasks.run_pipeline_job(None, str(job_id))

    input_file, job_out = paths["files"]
    assert (await _get_job(job_id)).status == "failed"
    assert not input_file.exists()
    assert not job_out.exists()


async def test_scratch_files_removed_after_cancellation(stub_pipeline, comfy_dirs):
    inp, out = comfy_dirs
    paths = {}

    async def behaviour(pipeline, job_id):
        paths["files"] = _leave_scratch_files(inp, out, job_id)
        await asyncio.sleep(30)

    holder = stub_pipeline(behaviour)
    job_id = await _make_job()

    task = asyncio.create_task(tasks.run_pipeline_job(None, str(job_id)))

    async def _wait_until_running():
        while "pipeline" not in holder:
            await asyncio.sleep(0.01)
        await holder["pipeline"].started.wait()

    await asyncio.wait_for(_wait_until_running(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    input_file, job_out = paths["files"]
    assert not input_file.exists()
    assert not job_out.exists()


async def test_cleanup_failure_does_not_fail_the_job(stub_pipeline, monkeypatch, tmp_path):
    """Tidying up is best effort -- it must never turn a good job bad."""
    final = tmp_path / "final.glb"
    final.write_bytes(b"glb")

    async def behaviour(pipeline, job_id):
        return {"final_path": final}

    stub_pipeline(behaviour)

    def boom(job_id, ext):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(tasks, "cleanup_job_files", boom)
    job_id = await _make_job()

    await tasks.run_pipeline_job(None, str(job_id))  # must not raise

    assert (await _get_job(job_id)).status == "succeeded"


async def test_cleanup_failure_does_not_mask_cancellation(stub_pipeline, monkeypatch):
    """An exception from a `finally` replaces whatever was propagating. If
    cleanup threw on the cancellation path, ARQ would never see the
    CancelledError it needs for its own bookkeeping."""
    async def behaviour(pipeline, job_id):
        await asyncio.sleep(30)

    holder = stub_pipeline(behaviour)

    def boom(job_id, ext):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(tasks, "cleanup_job_files", boom)
    job_id = await _make_job()

    task = asyncio.create_task(tasks.run_pipeline_job(None, str(job_id)))

    async def _wait_until_running():
        while "pipeline" not in holder:
            await asyncio.sleep(0.01)
        await holder["pipeline"].started.wait()

    await asyncio.wait_for(_wait_until_running(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await _get_job(job_id)).status == "failed"
