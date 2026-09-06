import json
import logging
import shutil
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

# PLAN.md sec.6 order. Durations are reported in this order regardless of the
# order nodes actually ran in, and it also defines "forward" for the reported
# stage label (see ProgressTracker.handle_executing).
STAGE_ORDER = [
    "segment_crop_fov",
    "structure_coarse_mesh",
    "shape_upsample",
    "texture_sample",
    "remesh_paint_final",
]
assert set(STAGE_ORDER) == set(STAGE_MAP.values())


class PipelineError(RuntimeError):
    pass


def job_input_filename(job_id: uuid.UUID, image_ext: str) -> str:
    """Name LoadImage reads. It resolves relative to ComfyUI's own input dir and
    cannot take an arbitrary path, hence a per-job file rather than a temp one."""
    return f"{job_id}{image_ext}"


def job_output_subdir(job_id: uuid.UUID) -> str:
    """Directory the two SaveGLB nodes write into, relative to the output dir."""
    return f"jobs/{job_id}"


def load_patched_graph(job_id: uuid.UUID, image_filename: str) -> dict:
    """Shared by both pipeline backends: same graph, same patch points."""
    graph = json.loads(GRAPH_PATH.read_text())
    subdir = job_output_subdir(job_id)
    graph["122"]["inputs"]["image"] = image_filename
    graph["1001"]["inputs"]["filename_prefix"] = f"{subdir}/coarse"
    graph["1002"]["inputs"]["filename_prefix"] = f"{subdir}/final"
    return graph


def cleanup_job_files(job_id: uuid.UUID, image_ext: str) -> None:
    """Delete a job's scratch files from ComfyUI's input and output volumes.

    Nothing here is the system of record: the input image came from object
    storage and the GLBs have been uploaded back to it by the time this runs.
    Without it every job leaked its input image plus ~8MB of GLBs into the
    `comfy_input`/`comfy_output` volumes permanently -- retention (PLAN.md
    sec.4) only ever knew about MinIO and Postgres.

    Best effort by design: this runs in a `finally`, and failing to tidy up must
    never turn a succeeded job into a failed one.
    """
    input_path = Path(settings.comfy_shared_input_dir) / job_input_filename(job_id, image_ext)
    output_dir = Path(settings.comfy_shared_output_dir) / job_output_subdir(job_id)

    try:
        input_path.unlink(missing_ok=True)
    except OSError:
        logger.warning("could not remove input file %s", input_path, exc_info=True)

    # ignore_errors: a partially-written output dir from a failed run is still
    # worth removing, and a missing one is the normal case for an early failure.
    shutil.rmtree(output_dir, ignore_errors=True)


def purge_orphaned_scratch_files() -> int:
    """Remove scratch files left by jobs from previous worker lifetimes.

    `cleanup_job_files` only tidies the job it ran for, so anything leaked
    before it existed -- or by a worker that was killed mid-job -- stays
    forever. Called once at startup, where max_jobs=1 guarantees nothing is in
    flight, exactly like `fail_orphaned_jobs`. Same caveat too: this assumes a
    single worker per volume, and would need revisiting for PLAN-VALOR.md F3.

    Only touches paths this service names itself: `jobs/<uuid>/` under the
    output dir and `<uuid>.<ext>` under the input dir. A file that isn't named
    for a job id is not ours and is left alone.
    """
    removed = 0

    jobs_root = Path(settings.comfy_shared_output_dir) / "jobs"
    if jobs_root.is_dir():
        for entry in jobs_root.iterdir():
            if not entry.is_dir() or not _is_job_id(entry.name):
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1

    input_root = Path(settings.comfy_shared_input_dir)
    if input_root.is_dir():
        for entry in input_root.iterdir():
            if not entry.is_file() or not _is_job_id(entry.stem):
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:
                logger.warning("could not remove stale input %s", entry, exc_info=True)

    if removed:
        logger.info("removed %d orphaned scratch file(s) from previous runs", removed)
    return removed


def _is_job_id(name: str) -> bool:
    try:
        uuid.UUID(name)
    except ValueError:
        return False
    return True


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

    def __init__(self, output_dir: Path, job_id: uuid.UUID, on_stage, on_artifact, clock=time.monotonic):
        self.output_dir = output_dir
        self.job_id = job_id
        self.on_stage = on_stage
        self.on_artifact = on_artifact
        self.seen_artifacts: set[str] = set()
        # Injectable so stage attribution can be tested against an exact
        # timeline instead of by sleeping.
        self.clock = clock

        self.run_start = self.clock()
        self.stage_start = self.run_start
        # Time is billed to whichever stage the running node belongs to...
        self.current_stage: str | None = None
        # ...but the label shown to the user only ever moves forward.
        self.reported_stage: str | None = None
        self.durations: dict[str, float] = {}

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

    def timings(self) -> list[dict]:
        """Accumulated per-stage seconds, in PLAN.md sec.6 order."""
        return [
            {"stage": stage, "seconds": round(self.durations[stage], 2)}
            for stage in STAGE_ORDER
            if stage in self.durations
        ]

    def total_seconds(self) -> float:
        return self.clock() - self.run_start

    def _close_open_stage(self, now: float) -> None:
        if self.current_stage is None:
            # Nothing open yet: leave stage_start alone so the interval before
            # the first node (executor startup, graph validation) carries into
            # the first stage instead of being discarded. That is what makes the
            # per-stage seconds add up to total_seconds.
            return
        self.durations[self.current_stage] = (
            self.durations.get(self.current_stage, 0.0) + now - self.stage_start
        )
        self.stage_start = now

    async def handle_executing(self, node: str | None) -> bool:
        """Feed one ``executing`` event's node id -- i.e. a node has just
        *started*.

        Timing keys off the start, not the finish. Keying off the finish (what
        this did before) billed each stage's elapsed time to the stage that
        followed it, never recorded the last stage at all, and left the UI
        naming the previous stage while the next one ran.

        Durations accumulate per stage rather than being written once on entry,
        because ComfyUI's `ux_friendly_pick_node` is free to schedule a later
        stage's node early -- a loader, say -- so a stage can be entered more
        than once. Returns True on the terminal ``node=None``.
        """
        now = self.clock()
        self._close_open_stage(now)

        if node is None:
            self.current_stage = None
            await self._report(self.reported_stage)
            return True

        stage = STAGE_MAP.get(node)
        if stage is None:
            # Unmapped node: its time keeps accruing to the stage already open.
            logger.debug("node %s has no stage mapping", node)
            return False

        self.current_stage = stage
        if self._is_forward(stage):
            self.reported_stage = stage
            await self._report(stage)
        return False

    def _is_forward(self, stage: str) -> bool:
        if self.reported_stage is None:
            return True
        return STAGE_ORDER.index(stage) > STAGE_ORDER.index(self.reported_stage)

    async def _report(self, stage: str | None) -> None:
        await self.on_stage(stage, self.timings(), self.total_seconds())

    async def finish(self) -> None:
        """Terminal signal: close the open stage and publish final timings.

        The executor never emits a terminal ``executing {node: None}`` -- that
        comes from main.py's `prompt_worker` loop (main.py:406), which the
        embedded backend bypasses. So the embedded backend takes the end of the
        run from ``execution_success`` instead, and both funnel through here.
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

        async def _noop_stage(stage: str | None, timings: list, total_seconds: float) -> None:
            return None

        async def _noop_artifact(name: str, path: Path) -> None:
            return None

        self.on_stage = on_stage or _noop_stage
        self.on_artifact = on_artifact or _noop_artifact

    async def run(self, job_id: uuid.UUID, image_bytes: bytes, image_ext: str) -> dict:
        image_filename = job_input_filename(job_id, image_ext)
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
    """PLAN.md sec.7.3: gltf-transform (meshopt) as a post-step, run in the worker.

    Calls the binary installed in the image rather than `npx --yes
    @gltf-transform/cli`, which re-resolved the package against the npm
    registry on every job -- unpinned, and a hard runtime network dependency
    that once stalled a job for five minutes before failing it.

    Errors carry the tool's stderr: the previous CalledProcessError said only
    "returned non-zero exit status 1", which explained nothing.
    """
    command = [settings.gltf_transform_bin, "meshopt", str(src), str(dst)]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=settings.gltf_transform_timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise PipelineError(
            f"{settings.gltf_transform_bin} not found -- it is installed globally in the "
            "worker image; set GLTF_TRANSFORM_BIN to override"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(
            f"gltf-transform timed out after {settings.gltf_transform_timeout_seconds}s"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise PipelineError(f"gltf-transform failed (exit {exc.returncode}): {stderr[-500:]}") from exc
