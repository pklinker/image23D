import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import websockets

from common.settings import settings

logger = logging.getLogger(__name__)

GRAPH_PATH = Path(__file__).resolve().parents[1] / "usain-bolt.pruned.api.json"

# PLAN.md sec.6's five stages, mapped onto the pruned graph's node ids.
STAGE_MAP = {
    **dict.fromkeys(["193", "192", "248", "312", "55", "56", "242", "15", "302", "303", "122"], "segment_crop_fov"),
    **dict.fromkeys(["319", "199", "125", "108", "279", "126", "87", "298", "3", "119", "4", "247", "1001"], "structure_coarse_mesh"),
    **dict.fromkeys(["91", "18", "94", "23", "92"], "shape_upsample"),
    **dict.fromkeys(["98", "12", "93"], "texture_sample"),
    **dict.fromkeys(["202", "241", "186", "238", "252", "282", "1002"], "remesh_paint_final"),
}


class PipelineError(RuntimeError):
    pass


def load_patched_graph(job_id: uuid.UUID, image_filename: str) -> dict:
    """Shared by both pipeline backends: same graph, same patch points."""
    graph = json.loads(GRAPH_PATH.read_text())
    graph["122"]["inputs"]["image"] = image_filename
    graph["1001"]["inputs"]["filename_prefix"] = f"jobs/{job_id}/coarse"
    graph["1002"]["inputs"]["filename_prefix"] = f"jobs/{job_id}/final"
    return graph


# Nodes whose saved file is surfaced as an artifact mid-run. Only the coarse
# preview: the final GLB is picked up from the pipeline's return value instead.
ARTIFACT_NODES = {"1001": "coarse"}


class ProgressTracker:
    """Turns ComfyUI's execution events into PLAN.md sec.6 stage transitions and
    the early coarse-mesh artifact callback.

    Both backends feed this the same two events now:

    - ``executing`` fires as each node *starts*. ComfyUI's executor runs nodes
      strictly one at a time, so seeing the next node start means the previous
      one has returned and its file writes are complete.
    - ``executed`` fires only for nodes that produce UI output -- in this graph
      just the two SaveGLB nodes -- and carries the filename actually written.

    Both are gated on ``server.client_id`` being set (execution.py:494 and :577),
    which is why the embedded backend saw neither until it started passing one.
    """

    def __init__(self, output_dir: Path, job_id: uuid.UUID, on_stage, on_artifact):
        self.output_dir = output_dir
        self.job_id = job_id
        self.on_stage = on_stage
        self.on_artifact = on_artifact
        self.seen_stages: set[str] = set()
        self.stage_start = time.monotonic()
        self.finished_node: str | None = None
        self.seen_finished: set[str] = set()
        self.seen_artifacts: set[str] = set()

    def _saved_path(self, output: dict) -> Path | None:
        entries = (output or {}).get("3d") or []
        if not entries:
            return None
        entry = entries[0]
        if "filename" not in entry:
            return None
        return self.output_dir / entry.get("subfolder", "") / entry["filename"]

    async def handle_executed(self, node: str | None, output: dict) -> None:
        """A node produced output.

        For the coarse SaveGLB this is both the earliest moment the file exists
        and the only reliable source of its name. The name used to be hardcoded
        as ``coarse_00001_.glb``, but SaveGLB derives its counter from
        ``folder_paths.get_save_image_path``, which scans the target directory --
        so ``_00001_`` only holds for the first write into a fresh folder.
        """
        if node is None or node not in ARTIFACT_NODES or node in self.seen_artifacts:
            return
        path = self._saved_path(output)
        if path is None:
            logger.warning("node %s finished with no 3d output to publish: %r", node, output)
            return
        self.seen_artifacts.add(node)
        await self.on_artifact(ARTIFACT_NODES[node], path)

    async def _node_finished(self, node: str) -> None:
        stage = STAGE_MAP.get(node)
        if stage and stage not in self.seen_stages:
            self.seen_stages.add(stage)
            now = time.monotonic()
            await self.on_stage(stage, now - self.stage_start)
            self.stage_start = now

    async def handle_executing(self, node: str | None) -> bool:
        """Feed one ``executing`` event's node id. Returns True once the run is
        over -- signalled by ``node=None``, which only the http backend sees
        (see `finish`)."""
        if self.finished_node and self.finished_node not in self.seen_finished:
            self.seen_finished.add(self.finished_node)
            await self._node_finished(self.finished_node)
        self.finished_node = node
        return node is None

    async def finish(self) -> None:
        """Terminal signal: flush the last started node's completion.

        The executor itself never emits a terminal ``executing {node: None}`` --
        that comes from main.py's `prompt_worker` loop (main.py:406), which the
        embedded backend bypasses by calling PromptExecutor directly. So the
        embedded backend takes the end of the run from ``execution_success``
        instead, and both funnel through here.
        """
        await self.handle_executing(None)


class ComfyHttpPipeline:
    """PLAN.md sec.5 Phase 1: POST the pruned API-format graph to ComfyUI's
    /prompt, subscribe to /ws for progress. No reimplementation of node logic.

    Kept as a fallback/debugging path after the Phase 4 embedded swap -- it
    only works if something is actually listening at settings.comfy_base_url,
    which the default docker-compose stack no longer runs continuously."""

    def __init__(self, on_stage=None, on_artifact=None):
        self.base_url = settings.comfy_base_url
        self.input_dir = Path(settings.comfy_shared_input_dir)
        self.output_dir = Path(settings.comfy_shared_output_dir)

        async def _noop_stage(stage: str, seconds: float) -> None:
            return None

        async def _noop_artifact(name: str, path: Path) -> None:
            return None

        self.on_stage = on_stage or _noop_stage
        self.on_artifact = on_artifact or _noop_artifact

    async def run(self, job_id: uuid.UUID, image_bytes: bytes, image_ext: str) -> dict:
        image_filename = f"{job_id}{image_ext}"
        (self.input_dir / image_filename).write_bytes(image_bytes)
        graph = load_patched_graph(job_id, image_filename)

        client_id = str(uuid.uuid4())
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")

        # Subscribe before submitting. Connecting after the POST races the
        # executor: any node that finishes in that window is invisible, which
        # for a fast node like the coarse SaveGLB means a lost artifact.
        async with websockets.connect(f"{ws_url}/ws?clientId={client_id}", max_size=None) as ws:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
                resp = await client.post("/prompt", json={"prompt": graph, "client_id": client_id})
                resp.raise_for_status()
                body = resp.json()
                if body["node_errors"]:
                    raise PipelineError(f"ComfyUI rejected graph: {body['node_errors']}")
                prompt_id = body["prompt_id"]

            await self._stream_progress(ws, prompt_id, job_id)

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            resp = await client.get(f"/history/{prompt_id}")
            resp.raise_for_status()
            history = resp.json()[prompt_id]

        if history["status"]["status_str"] != "success":
            raise PipelineError(f"ComfyUI job failed: {history['status']}")

        final_rel = history["outputs"]["1002"]["3d"][0]
        return {"final_path": self.output_dir / final_rel["subfolder"] / final_rel["filename"]}

    async def _stream_progress(self, ws, prompt_id: str, job_id: uuid.UUID) -> None:
        tracker = ProgressTracker(self.output_dir, job_id, self.on_stage, self.on_artifact)
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            message = json.loads(raw)
            data = message.get("data") or {}
            if data.get("prompt_id") not in (prompt_id, None):
                continue

            event = message["type"]
            if event == "executing":
                # Terminal `node: None` comes from main.py's prompt_worker loop,
                # which only exists on this (server) path.
                if await tracker.handle_executing(data.get("node")):
                    break
            elif event == "executed":
                await tracker.handle_executed(data.get("node"), data.get("output") or {})
            elif event == "execution_success":
                await tracker.finish()
                break
            elif event in ("execution_error", "execution_interrupted"):
                raise PipelineError(f"ComfyUI {event}: {data}")


def compress_glb(src: Path, dst: Path) -> None:
    """PLAN.md sec.7.3: gltf-transform (meshopt) as a post-step, run in the worker."""
    subprocess.run(
        ["npx", "--yes", "@gltf-transform/cli", "meshopt", str(src), str(dst)],
        check=True,
        capture_output=True,
        env={**os.environ, "npm_config_yes": "true"},
    )
