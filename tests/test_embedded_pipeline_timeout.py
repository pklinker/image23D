"""Pipeline deadline and interrupt tests -- PLAN-BUGFIX.md item 1.

The property under test is the one that makes the deadline work at all: the
GPU run happens in an `asyncio.to_thread`, so cancelling its task marks the task
cancelled while the thread keeps running and keeps holding the card. The only
thing that ends a run is ComfyUI's cooperative interrupt flag, so the pipeline
must raise that flag and then *wait for the thread*, never cancel it.

ComfyUI itself is not importable here (no GPU, no weights), so `_set_interrupt`
is stubbed and the executor thread is stood in for by a task that watches the
same flag -- which is exactly how the real one behaves.
"""
import asyncio

import pytest

from common.settings import settings
from worker.app import embedded_pipeline as ep


@pytest.fixture
def interrupt_flag(monkeypatch):
    """Replaces ComfyUI's global interrupt flag with an observable one."""
    flag = {"value": False, "calls": []}

    def fake_set_interrupt(value: bool) -> None:
        flag["value"] = value
        flag["calls"].append(value)

    monkeypatch.setattr(ep, "_set_interrupt", fake_set_interrupt)
    return flag


@pytest.fixture
def pipeline():
    return ep.ComfyEmbeddedPipeline()


def _fake_comfy_thread(flag, *, poll=0.02, finishes_after=None, outputs=None):
    """Stands in for `_execute_prompt_sync` running in a worker thread.

    Mirrors the real executor: it notices the interrupt flag only between steps,
    and reports an interrupted run as a failed one (`executor.success` is False,
    which `_execute_prompt_sync` turns into a PipelineError).
    """
    async def body():
        elapsed = 0.0
        while True:
            if flag["value"]:
                raise ep.PipelineError("ComfyUI execution failed: [('execution_interrupted', ...)]")
            if finishes_after is not None and elapsed >= finishes_after:
                return outputs
            await asyncio.sleep(poll)
            elapsed += poll

    return asyncio.create_task(body())


async def test_deadline_interrupts_and_waits_for_the_thread(pipeline, interrupt_flag, monkeypatch):
    monkeypatch.setattr(settings, "pipeline_timeout_seconds", 0.2)
    queue: asyncio.Queue = asyncio.Queue()
    execute_future = _fake_comfy_thread(interrupt_flag)

    with pytest.raises(ep.PipelineTimeout) as excinfo:
        await pipeline._drain_progress(queue, execute_future, "job-1")

    assert "0.2s deadline" in str(excinfo.value)
    assert interrupt_flag["calls"][0] is True, "the interrupt flag must be raised"
    # The decisive assertion: the stand-in thread ran to completion rather than
    # being cancelled out from under us. Cancelling would abandon a live GPU run.
    assert execute_future.done() and not execute_future.cancelled()
    # And the flag is left clear so it cannot leak onto the next job.
    assert interrupt_flag["value"] is False


async def test_no_interrupt_when_the_run_finishes_in_time(pipeline, interrupt_flag, monkeypatch):
    monkeypatch.setattr(settings, "pipeline_timeout_seconds", 30)
    queue: asyncio.Queue = asyncio.Queue()
    expected = {"1002": {"3d": [{"subfolder": "jobs/x", "filename": "final_00001_.glb"}]}}
    execute_future = _fake_comfy_thread(interrupt_flag, finishes_after=0.05, outputs=expected)

    outputs = await pipeline._drain_progress(queue, execute_future, "job-2")

    assert outputs == expected
    assert interrupt_flag["calls"] == [], "nothing should have been interrupted"


async def test_execution_error_does_not_cancel_the_thread(pipeline, interrupt_flag, monkeypatch):
    """The old code called execute_future.cancel() here, abandoning the thread."""
    monkeypatch.setattr(settings, "pipeline_timeout_seconds", 30)
    queue: asyncio.Queue = asyncio.Queue()
    execute_future = _fake_comfy_thread(interrupt_flag, finishes_after=0.3, outputs=None)
    queue.put_nowait(("execution_error", {"node_id": "18"}, None))

    with pytest.raises(ep.PipelineError):
        await pipeline._drain_progress(queue, execute_future, "job-3")

    assert execute_future.done() and not execute_future.cancelled()


async def test_cancellation_interrupts_and_joins(pipeline, interrupt_flag):
    """`_interrupt_and_join` is the path ARQ takes on worker shutdown."""
    execute_future = _fake_comfy_thread(interrupt_flag)

    await pipeline._interrupt_and_join(execute_future)

    assert interrupt_flag["calls"][0] is True
    assert execute_future.done() and not execute_future.cancelled()
    assert interrupt_flag["value"] is False


async def test_interrupt_join_gives_up_rather_than_hanging(pipeline, interrupt_flag, monkeypatch):
    """A thread that ignores the interrupt must not wedge the worker forever."""
    monkeypatch.setattr(settings, "pipeline_interrupt_grace_seconds", 0.1)

    async def deaf_thread():
        await asyncio.sleep(30)

    execute_future = asyncio.create_task(deaf_thread())
    try:
        await asyncio.wait_for(pipeline._interrupt_and_join(execute_future), timeout=5)
        assert not execute_future.done(), "still running -- we gave up on it, not cancelled it"
    finally:
        execute_future.cancel()
