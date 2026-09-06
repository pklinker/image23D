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
import sys
import threading
import uuid
from pathlib import Path

from common.settings import settings
from worker.app.pipeline import ProgressTracker, load_patched_graph

COMFY_ROOT = "/app/ComfyUI"

_bootstrap_lock = asyncio.Lock()
_bootstrapped = False


class PipelineError(RuntimeError):
    pass


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
    executor.execute(prompt, prompt_id, {}, execute_outputs)
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

        async def _noop_stage(stage: str, seconds: float) -> None:
            return None

        async def _noop_artifact(name: str, path: Path) -> None:
            return None

        self.on_stage = on_stage or _noop_stage
        self.on_artifact = on_artifact or _noop_artifact

    async def run(self, job_id: uuid.UUID, image_bytes: bytes, image_ext: str) -> dict:
        await bootstrap_comfy()

        import execution
        import server as comfy_server

        image_filename = f"{job_id}{image_ext}"
        (self.input_dir / image_filename).write_bytes(image_bytes)
        graph = load_patched_graph(job_id, image_filename)

        prompt_id = str(uuid.uuid4())
        valid = await execution.validate_prompt(prompt_id, graph, None)
        success, error, execute_outputs, node_errors = valid
        if not success:
            raise PipelineError(f"ComfyUI rejected graph: {error} {node_errors}")

        stub = comfy_server.PromptServer.instance
        execute_future = asyncio.create_task(
            asyncio.to_thread(_execute_prompt_sync, graph, prompt_id, execute_outputs)
        )
        outputs = await self._drain_progress(stub.messages, execute_future, job_id)

        final_rel = outputs["1002"]["3d"][0]
        return {"final_path": self.output_dir / final_rel["subfolder"] / final_rel["filename"]}

    async def _drain_progress(self, queue: asyncio.Queue, execute_future: asyncio.Task, job_id: uuid.UUID) -> dict:
        # This ComfyUI version never emits the legacy "executing" event for
        # this graph (confirmed by direct observation, see PHASE4.md) --
        # track progress off "progress_state" instead. See
        # ProgressTracker.handle_progress_state's docstring for why.
        tracker = ProgressTracker(self.output_dir, job_id, self.on_stage, self.on_artifact)
        while not execute_future.done():
            try:
                event, data, _sid = await asyncio.wait_for(queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if event == "progress_state":
                await tracker.handle_progress_state(data.get("nodes", {}))
            elif event == "execution_error":
                # execute_future will also raise from its own exception
                # propagation; this just fails fast instead of waiting.
                execute_future.cancel()
                raise PipelineError(f"ComfyUI execution error: {data}")
        return await execute_future
