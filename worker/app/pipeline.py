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


class ComfyHttpPipeline:
    """PLAN.md sec.5 Phase 1: POST the pruned API-format graph to ComfyUI's
    /prompt, subscribe to /ws for progress. No reimplementation of node logic."""

    def __init__(self, on_stage=None):
        self.base_url = settings.comfy_base_url
        self.input_dir = Path(settings.comfy_shared_input_dir)
        self.output_dir = Path(settings.comfy_shared_output_dir)

        async def _noop(stage: str, seconds: float) -> None:
            return None

        self.on_stage = on_stage or _noop

    async def run(self, job_id: uuid.UUID, image_bytes: bytes, image_ext: str) -> dict:
        image_filename = f"{job_id}{image_ext}"
        (self.input_dir / image_filename).write_bytes(image_bytes)

        graph = json.loads(GRAPH_PATH.read_text())
        graph["122"]["inputs"]["image"] = image_filename
        graph["1001"]["inputs"]["filename_prefix"] = f"jobs/{job_id}/coarse"
        graph["1002"]["inputs"]["filename_prefix"] = f"jobs/{job_id}/final"

        client_id = str(uuid.uuid4())
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            resp = await client.post("/prompt", json={"prompt": graph, "client_id": client_id})
            resp.raise_for_status()
            body = resp.json()
            if body["node_errors"]:
                raise PipelineError(f"ComfyUI rejected graph: {body['node_errors']}")
            prompt_id = body["prompt_id"]

        await self._stream_progress(prompt_id, client_id)

        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            resp = await client.get(f"/history/{prompt_id}")
            resp.raise_for_status()
            history = resp.json()[prompt_id]

        if history["status"]["status_str"] != "success":
            raise PipelineError(f"ComfyUI job failed: {history['status']}")

        outputs = history["outputs"]
        coarse_rel = outputs["1001"]["3d"][0]
        final_rel = outputs["1002"]["3d"][0]
        return {
            "coarse_path": self.output_dir / coarse_rel["subfolder"] / coarse_rel["filename"],
            "final_path": self.output_dir / final_rel["subfolder"] / final_rel["filename"],
        }

    async def _stream_progress(self, prompt_id: str, client_id: str) -> None:
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        seen_stages = set()
        stage_start = time.monotonic()
        async with websockets.connect(f"{ws_url}/ws?clientId={client_id}", max_size=None) as ws:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                data = json.loads(raw)
                if data.get("data", {}).get("prompt_id") not in (prompt_id, None):
                    continue
                if data["type"] == "executing":
                    node = data["data"]["node"]
                    if node is None:
                        break
                    stage = STAGE_MAP.get(node)
                    if stage and stage not in seen_stages:
                        seen_stages.add(stage)
                        now = time.monotonic()
                        await self.on_stage(stage, now - stage_start)
                        stage_start = now
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
