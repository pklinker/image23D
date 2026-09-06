"""PLAN.md sec.5 Phase 2 / sec.4 hardening: import ComfyUI as a library and
execute the pruned graph in-process, no HTTP/WS hop to a separate server.

This reuses ComfyUI's own executor/topological-sort/validation engine
(execution.py's validate_prompt + PromptExecutor) rather than hand-porting
each of the ~30 node classes' Python calls ourselves -- that was the whole
point of running on top of ComfyUI in the first place (PLAN.md sec.5:
"zero reimplementation risk"). What we replace is just the transport: no
aiohttp app, no websocket routes, no frontend -- see PHASE4.md for how this
was scoped out by reading execution.py/server.py/folder_paths.py directly.
"""
import asyncio
import logging
import sys
import time
import uuid
from pathlib import Path

from common.settings import settings
from worker.app.pipeline import ProgressTracker, job_input_filename, load_patched_graph

COMFY_ROOT = "/app/ComfyUI"

# Any non-None value will do. execution.py gates the `executing` and `executed`
# events on `server.client_id is not None` (:494, :577), and `execute_async`
# sets that field from extra_data["client_id"] -- so an empty extra_data, as
# this passed originally, silently disabled both. That was the whole of
# PHASE4.md's "the executing event never arrives" mystery.
EMBEDDED_CLIENT_ID = "embedded"

_bootstrap_lock = asyncio.Lock()
_bootstrapped = False


class PipelineError(RuntimeError):
    pass


class PipelineTimeout(PipelineError):
    """The run exceeded settings.pipeline_timeout_seconds and was interrupted."""


def _set_interrupt(value: bool) -> None:
    """Raise/clear ComfyUI's cooperative interrupt flag.

    This is the only thing that actually stops an in-flight run. It is checked
    between nodes and inside sampler steps, and `throw_exception_if_processing_interrupted`
    clears it as it fires, so it cannot leak onto the next prompt (which resets
    it again at the top of `execute_async` regardless).
    """
    import nodes

    nodes.interrupt_processing(value)


class _StubServer:
    """Just enough of server.PromptServer's surface for execution.py to talk
    to: send_sync (executing/execution_error/execution_success events) and
    send_progress_text (used directly by GetMeshInfo, one of our 41 nodes).
    No aiohttp, no websocket routes, no frontend -- nothing here needs them.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.client_id = "embedded"
        self.last_node_id = None
        self.loop = loop
        self.messages: asyncio.Queue = asyncio.Queue()

    def send_sync(self, event, data, sid=None) -> None:
        # May be called from a different thread than self.loop runs on (the
        # PromptExecutor.execute() call happens in a worker thread -- see
        # ComfyEmbeddedPipeline.run below) -- call_soon_threadsafe is the
        # real PromptServer's own mechanism for exactly this, not something
        # invented for this stub.
        self.loop.call_soon_threadsafe(self.messages.put_nowait, (event, data, sid))

    def send_progress_text(self, text, node_id, sid=None) -> None:
        self.send_sync("progress_text", {"node_id": node_id, "text": text}, sid)


def _reset_gpu_peak() -> None:
    """Start a fresh peak-memory window for this run."""
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:  # noqa: BLE001 -- telemetry must never fail a job
        logging.debug("could not reset CUDA peak memory stats", exc_info=True)


def _gpu_peak_mb() -> int | None:
    """Peak of the *torch allocator* during this run, in MiB.

    Not the same number nvidia-smi reports: it excludes the CUDA context and
    any allocation made outside torch, so it reads lower than PHASE1.md's
    12,725 MiB. It is comparable across runs, which is what makes it useful.
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        return int(torch.cuda.max_memory_allocated() / (1024 * 1024))
    except Exception:  # noqa: BLE001 -- telemetry must never fail a job
        logging.debug("could not read CUDA peak memory", exc_info=True)
        return None


async def bootstrap_comfy() -> None:
    """Idempotent, safe to call at the top of every job -- real work only
    happens once per process. Call from ARQ's on_startup hook so it runs
    before the first job, on the loop that will service all jobs."""
    global _bootstrapped
    async with _bootstrap_lock:
        if _bootstrapped:
            return

        if COMFY_ROOT not in sys.path:
            sys.path.insert(0, COMFY_ROOT)

        import cuda_malloc  # noqa: F401  -- must precede the first `import torch` anywhere in this process
        import nodes
        import server as comfy_server

        comfy_server.PromptServer.instance = _StubServer(asyncio.get_running_loop())
        # init_custom_nodes=False: no custom node packs in usain-bolt's graph
        # (PLAN.md sec.1), so skip scanning custom_nodes/ entirely.
        await nodes.init_extra_nodes(init_custom_nodes=False, init_api_nodes=False)
        _bootstrapped = True


def _execute_prompt_sync(prompt: dict, prompt_id: str, execute_outputs: list) -> dict:
    """Runs in a worker thread (see ComfyEmbeddedPipeline.run) -- both
    validate_prompt and PromptExecutor.execute() manage their own asyncio.run
    internally and must not be called from a thread that already has a
    running loop."""
    import execution
    import server as comfy_server

    executor = execution.PromptExecutor(
        comfy_server.PromptServer.instance,
        cache_type=execution.CacheType.NONE,
        cache_args={"lru": 0, "ram": 0, "ram_inactive": 0},
    )
    executor.execute(prompt, prompt_id, {"client_id": EMBEDDED_CLIENT_ID}, execute_outputs)
    if not executor.success:
        raise PipelineError(f"ComfyUI execution failed: {executor.status_messages}")
    return executor.history_result["outputs"]


class ComfyEmbeddedPipeline:
    """Same interface as ComfyHttpPipeline (PLAN.md sec.5's one Protocol,
    two implementations), swapped in as the default in Phase 4. Must run in
    the same process/container as ComfyUI's own code and weights -- see
    docker/worker.Dockerfile, which now merges what used to be the separate
    comfy-worker (GPU/HTTP server) and worker (CPU/ARQ) images."""

    def __init__(self, on_stage=None, on_artifact=None):
        self.input_dir = Path(settings.comfy_shared_input_dir)
        self.output_dir = Path(settings.comfy_shared_output_dir)

        async def _noop_stage(stage: str | None, timings: list, total_seconds: float) -> None:
            return None

        async def _noop_artifact(name: str, path: Path) -> None:
            return None

        self.on_stage = on_stage or _noop_stage
        self.on_artifact = on_artifact or _noop_artifact

    async def run(self, job_id: uuid.UUID, image_bytes: bytes, image_ext: str) -> dict:
        await bootstrap_comfy()

        import execution
        import server as comfy_server

        image_filename = job_input_filename(job_id, image_ext)
        (self.input_dir / image_filename).write_bytes(image_bytes)
        graph = load_patched_graph(job_id, image_filename)

        prompt_id = str(uuid.uuid4())
        valid = await execution.validate_prompt(prompt_id, graph, None)
        success, error, execute_outputs, node_errors = valid
        if not success:
            raise PipelineError(f"ComfyUI rejected graph: {error} {node_errors}")

        stub = comfy_server.PromptServer.instance
        _reset_gpu_peak()
        execute_future = asyncio.create_task(
            asyncio.to_thread(_execute_prompt_sync, graph, prompt_id, execute_outputs)
        )
        try:
            outputs = await self._drain_progress(stub.messages, execute_future, job_id)
        except asyncio.CancelledError:
            # ARQ cancels the job task on worker shutdown or an explicit abort.
            # Unwinding on cancellation alone would leave the ComfyUI thread
            # running with the GPU still allocated, so stop the run for real
            # and wait for the thread before letting the cancellation through.
            await self._interrupt_and_join(execute_future)
            raise

        final_rel = outputs["1002"]["3d"][0]
        return {
            "final_path": self.output_dir / final_rel["subfolder"] / final_rel["filename"],
            "gpu_peak_mb": _gpu_peak_mb(),
        }

    async def _handle_event(self, tracker: ProgressTracker, event: str, data: dict):
        """Dispatch one executor event. Returns the (event, data) pair if it
        signals a failed run, else None."""
        if event == "executing":
            await tracker.handle_executing(data.get("node"))
        elif event == "executed":
            await tracker.handle_executed(data.get("node"), data.get("output") or {})
        elif event == "execution_success":
            # The executor never emits a terminal `executing {node: None}`; that
            # comes from main.py's prompt_worker loop, which this backend
            # bypasses by driving PromptExecutor directly.
            await tracker.finish()
        elif event in ("execution_error", "execution_interrupted"):
            return (event, data)
        return None

    async def _drain_progress(self, queue: asyncio.Queue, execute_future: asyncio.Task, job_id: uuid.UUID) -> dict:
        tracker = ProgressTracker(self.output_dir, job_id, self.on_stage, self.on_artifact)
        deadline = time.monotonic() + settings.pipeline_timeout_seconds
        interrupted = False
        ended_early = False
        error_event: tuple[str, dict] | None = None

        while not execute_future.done():
            if not interrupted and time.monotonic() >= deadline:
                # The deadline is enforced here rather than by ARQ's job_timeout
                # because ARQ times a job out by cancelling its task, and the
                # GPU work runs in an asyncio.to_thread that cancellation cannot
                # stop. Raising ComfyUI's interrupt flag is the only thing that
                # ends the run, and doing it here means the failure is recorded
                # normally instead of from inside a cancellation handler.
                interrupted = True
                logging.warning(
                    "job %s exceeded the %ss pipeline deadline; interrupting ComfyUI",
                    job_id, settings.pipeline_timeout_seconds,
                )
                _set_interrupt(True)

            try:
                event, data, _sid = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue

            error_event = await self._handle_event(tracker, event, data)
            if error_event is not None:
                # Stop consuming progress, but deliberately do not cancel
                # execute_future: it wraps asyncio.to_thread, so cancelling
                # abandons the thread rather than stopping it. The executor is
                # already unwinding -- awaiting it below surfaces the real error
                # and confirms the thread actually exited.
                logging.info("job %s: ComfyUI reported %s", job_id, error_event[0])
                ended_early = True
                break

        # The loop above exits the moment execute_future completes, which can
        # happen before the last events have been consumed -- notably the
        # terminal execution_success. Drain whatever is left so the final node's
        # completion is not silently dropped.
        while error_event is None and not queue.empty():
            event, data, _sid = queue.get_nowait()
            error_event = await self._handle_event(tracker, event, data)

        if ended_early:
            # Bounded: the executor emits these events as it unwinds and returns
            # immediately afterwards. If it somehow doesn't, say so rather than
            # hanging the worker forever.
            try:
                await asyncio.wait_for(
                    asyncio.shield(execute_future),
                    timeout=settings.pipeline_interrupt_grace_seconds,
                )
            except asyncio.TimeoutError:
                raise PipelineError(
                    "ComfyUI reported a failure but its worker thread did not exit within "
                    f"{settings.pipeline_interrupt_grace_seconds}s"
                ) from None
            except Exception:
                pass  # re-raised with full context by the await below

        timeout_message = (
            f"pipeline exceeded its {settings.pipeline_timeout_seconds}s deadline and was interrupted"
        )

        try:
            outputs = await execute_future
        except PipelineError:
            if interrupted:
                raise PipelineTimeout(timeout_message) from None
            raise
        finally:
            if interrupted:
                # Clear the flag if the run finished before consuming it --
                # `throw_exception_if_processing_interrupted` only clears it when
                # it actually fires.
                _set_interrupt(False)

        if error_event is not None:
            # The executor sets success=False after reporting one of these, so
            # the await above normally raises and we never get here. Belt and
            # braces: never hand back a result for a run that reported a
            # failure, and keep the event payload, which names the failing node.
            if interrupted:
                raise PipelineTimeout(timeout_message)
            event, data = error_event
            raise PipelineError(f"ComfyUI {event}: {data}")

        return outputs

    async def _interrupt_and_join(self, execute_future: asyncio.Task) -> None:
        """Stop an in-flight run and wait for its thread to actually exit.

        Cancelling `execute_future` would not do this. It wraps
        asyncio.to_thread: cancellation marks the task cancelled while the
        thread keeps running, holding ~12.9GB of VRAM. Only ComfyUI's
        cooperative interrupt flag ends the run, and the thread then has to be
        given time to notice it.
        """
        _set_interrupt(True)
        try:
            await asyncio.wait_for(
                asyncio.shield(execute_future),
                timeout=settings.pipeline_interrupt_grace_seconds,
            )
        except asyncio.TimeoutError:
            logging.error(
                "ComfyUI thread still running %ss after interrupt; the GPU may stay busy",
                settings.pipeline_interrupt_grace_seconds,
            )
        except asyncio.CancelledError:
            logging.error("cancelled again while waiting for the ComfyUI thread to stop")
        except Exception:
            # Includes the interrupt surfacing as a failed execution, which is
            # exactly what we asked for.
            logging.info("ComfyUI run ended while being interrupted", exc_info=True)
        finally:
            _set_interrupt(False)
