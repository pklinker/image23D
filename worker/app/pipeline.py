import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import websockets

from common.settings import settings

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


class ProgressTracker:
    """Turns ComfyUI's node-progress events into PLAN.md sec.6 stage
    transitions and the early coarse-mesh artifact callback. Shared by both
    pipeline backends -- but they don't actually get the same event shape
    (see notes on each handle_* method), so this holds two independent entry
    points rather than one, each doing the same seen-stages/on_artifact
    bookkeeping against a different underlying signal."""

    def __init__(self, output_dir: Path, job_id: uuid.UUID, on_stage, on_artifact):
        self.output_dir = output_dir
        self.job_id = job_id
        self.on_stage = on_stage
        self.on_artifact = on_artifact
        self.seen_stages: set[str] = set()
        self.stage_start = time.monotonic()
        self.finished_node: str | None = None
        self.seen_finished: set[str] = set()

    async def _node_finished(self, node: str) -> None:
        if node == "1001":
            coarse_path = self.output_dir / f"jobs/{self.job_id}" / "coarse_00001_.glb"
            await self.on_artifact("coarse", coarse_path)
        stage = STAGE_MAP.get(node)
        if stage and stage not in self.seen_stages:
            self.seen_stages.add(stage)
            now = time.monotonic()
            await self.on_stage(stage, now - self.stage_start)
            self.stage_start = now

    async def handle_executing(self, node: str | None) -> bool:
        """HTTP backend: feed one "executing" event's node id (the websocket
        message ComfyUI's server sends when a node *starts*). Returns True
        once the terminal `node=None` event (prompt fully finished) has been
        seen. ComfyUI's executor runs nodes strictly one at a time, so seeing
        the *next* node start means the previous one already returned --
        its file write is complete, not just queued."""
        if self.finished_node and self.finished_node not in self.seen_finished:
            self.seen_finished.add(self.finished_node)
            await self._node_finished(self.finished_node)
        self.finished_node = node
        return node is None

    async def handle_progress_state(self, nodes: dict) -> None:
        """Embedded backend: feed one "progress_state" event's full node-state
        snapshot ({node_id: {"state": "running"|"finished", ...}, ...}).
        This install of ComfyUI (v0.34.0-56-g250b2e95) never actually emits
        the legacy "executing" event for this graph -- see PHASE4.md for how
        that was confirmed -- so the embedded backend tracks progress off
        this newer, unrelated mechanism (comfy_execution/progress.py's
        WebUIProgressHandler) instead. It's a cumulative snapshot, not a
        stream of discrete transitions, so "finished" has to be diffed
        against what's already been seen rather than read as an edge."""
        for node_id, info in nodes.items():
            if info.get("state") == "finished" and node_id not in self.seen_finished:
                self.seen_finished.add(node_id)
                await self._node_finished(node_id)


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
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            resp = await client.post("/prompt", json={"prompt": graph, "client_id": client_id})
            resp.raise_for_status()
            body = resp.json()
            if body["node_errors"]:
                raise PipelineError(f"ComfyUI rejected graph: {body['node_errors']}")
            prompt_id = body["prompt_id"]

        await self._stream_progress(prompt_id, client_id, job_id)

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            resp = await client.get(f"/history/{prompt_id}")
            resp.raise_for_status()
            history = resp.json()[prompt_id]

        if history["status"]["status_str"] != "success":
            raise PipelineError(f"ComfyUI job failed: {history['status']}")

        final_rel = history["outputs"]["1002"]["3d"][0]
        return {"final_path": self.output_dir / final_rel["subfolder"] / final_rel["filename"]}

    async def _stream_progress(self, prompt_id: str, client_id: str, job_id: uuid.UUID) -> None:
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        tracker = ProgressTracker(self.output_dir, job_id, self.on_stage, self.on_artifact)
        async with websockets.connect(f"{ws_url}/ws?clientId={client_id}", max_size=None) as ws:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                data = json.loads(raw)
                if data.get("data", {}).get("prompt_id") not in (prompt_id, None):
                    continue
                if data["type"] == "executing":
                    if await tracker.handle_executing(data["data"]["node"]):
                        break
                elif data["type"] == "execution_error":
                    raise PipelineError(f"ComfyUI execution error: {data['data']}")


def compress_glb(src: Path, dst: Path) -> None:
    """PLAN.md sec.7.3: gltf-transform (meshopt) as a post-step, run in the worker."""
    subprocess.run(
        ["npx", "--yes", "@gltf-transform/cli", "meshopt", str(src), str(dst)],
        check=True,
        capture_output=True,
        env={**os.environ, "npm_config_yes": "true"},
    )
