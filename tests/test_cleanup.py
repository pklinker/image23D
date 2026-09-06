"""Scratch-file cleanup tests -- PLAN-BUGFIX.md item 6.

Every job wrote its input image into ComfyUI's input volume and ~8MB of GLBs
into its output volume, and nothing ever removed them: retention only knew
about MinIO and Postgres.
"""
import uuid

import pytest

from common.settings import settings
from worker.app.pipeline import (
    cleanup_job_files,
    job_input_filename,
    job_output_subdir,
    load_patched_graph,
    purge_orphaned_scratch_files,
)


@pytest.fixture
def comfy_dirs(tmp_path, monkeypatch):
    """Point the shared input/output volumes at a temp dir."""
    inp = tmp_path / "input"
    out = tmp_path / "output"
    inp.mkdir()
    out.mkdir()
    monkeypatch.setattr(settings, "comfy_shared_input_dir", str(inp))
    monkeypatch.setattr(settings, "comfy_shared_output_dir", str(out))
    return inp, out


def _populate(inp, out, job_id, ext=".png"):
    """Lay down exactly what a real run leaves behind."""
    (inp / job_input_filename(job_id, ext)).write_bytes(b"png")
    job_out = out / job_output_subdir(job_id)
    job_out.mkdir(parents=True)
    (job_out / "coarse_00001_.glb").write_bytes(b"glb")
    (job_out / "final_00001_.glb").write_bytes(b"glb" * 1000)
    return job_out


def test_cleanup_removes_input_and_output(comfy_dirs):
    inp, out = comfy_dirs
    job_id = uuid.uuid4()
    job_out = _populate(inp, out, job_id)

    assert (inp / job_input_filename(job_id, ".png")).exists()
    assert job_out.exists()

    cleanup_job_files(job_id, ".png")

    assert not (inp / job_input_filename(job_id, ".png")).exists()
    assert not job_out.exists()


def test_cleanup_is_a_no_op_when_nothing_was_written(comfy_dirs):
    """An early failure (bad object key) never reaches the pipeline, so neither
    file exists -- cleanup still runs from the `finally` and must not raise."""
    cleanup_job_files(uuid.uuid4(), ".png")


def test_cleanup_leaves_other_jobs_alone(comfy_dirs):
    inp, out = comfy_dirs
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    _populate(inp, out, mine)
    other_out = _populate(inp, out, theirs)

    cleanup_job_files(mine, ".png")

    assert other_out.exists()
    assert (inp / job_input_filename(theirs, ".png")).exists()


def test_cleanup_removes_a_partial_output_dir(comfy_dirs):
    """A run that died after the coarse GLB still leaves a directory behind."""
    inp, out = comfy_dirs
    job_id = uuid.uuid4()
    job_out = out / job_output_subdir(job_id)
    job_out.mkdir(parents=True)
    (job_out / "coarse_00001_.glb").write_bytes(b"glb")

    cleanup_job_files(job_id, ".png")

    assert not job_out.exists()


def test_cleanup_honours_the_extension_actually_used(comfy_dirs):
    inp, out = comfy_dirs
    job_id = uuid.uuid4()
    (inp / job_input_filename(job_id, ".jpg")).write_bytes(b"jpg")

    cleanup_job_files(job_id, ".jpg")

    assert not (inp / job_input_filename(job_id, ".jpg")).exists()


def test_cleanup_paths_match_the_graph_the_worker_submits():
    """The paths cleanup deletes must be the ones SaveGLB was told to write.

    These are derived independently in load_patched_graph and cleanup_job_files;
    if they drift, cleanup silently stops removing anything.
    """
    job_id = uuid.uuid4()
    graph = load_patched_graph(job_id, job_input_filename(job_id, ".png"))

    subdir = job_output_subdir(job_id)
    assert graph["1001"]["inputs"]["filename_prefix"] == f"{subdir}/coarse"
    assert graph["1002"]["inputs"]["filename_prefix"] == f"{subdir}/final"
    assert graph["122"]["inputs"]["image"] == f"{job_id}.png"


# --- startup sweep of files leaked by earlier worker lifetimes -------------


def test_startup_sweep_removes_previous_jobs(comfy_dirs):
    """cleanup_job_files only tidies its own job, so anything leaked before it
    existed -- or by a worker killed mid-job -- needs collecting at startup."""
    inp, out = comfy_dirs
    old_jobs = [uuid.uuid4() for _ in range(3)]
    for job_id in old_jobs:
        _populate(inp, out, job_id)

    removed = purge_orphaned_scratch_files()

    assert removed == 6  # 3 output dirs + 3 input files
    for job_id in old_jobs:
        assert not (out / job_output_subdir(job_id)).exists()
        assert not (inp / job_input_filename(job_id, ".png")).exists()


def test_startup_sweep_leaves_foreign_files_alone(comfy_dirs):
    """Only paths this service names after a job id are ours to delete."""
    inp, out = comfy_dirs
    (inp / "example.png").write_bytes(b"png")
    (out / "3d").mkdir()
    (out / "3d" / "manual_run.glb").write_bytes(b"glb")
    (out / "jobs").mkdir()
    (out / "jobs" / "not-a-uuid").mkdir()

    assert purge_orphaned_scratch_files() == 0

    assert (inp / "example.png").exists()
    assert (out / "3d" / "manual_run.glb").exists()
    assert (out / "jobs" / "not-a-uuid").exists()


def test_startup_sweep_on_empty_volumes(comfy_dirs):
    assert purge_orphaned_scratch_files() == 0


def test_startup_sweep_tolerates_missing_dirs(tmp_path, monkeypatch):
    """First boot on a fresh volume: the dirs may not exist yet."""
    monkeypatch.setattr(settings, "comfy_shared_input_dir", str(tmp_path / "nope"))
    monkeypatch.setattr(settings, "comfy_shared_output_dir", str(tmp_path / "also-nope"))
    assert purge_orphaned_scratch_files() == 0


# --- compress_glb error reporting (PLAN-BUGFIX.md item 8) -----------------


def test_compress_glb_reports_stderr(monkeypatch, tmp_path):
    """The old failure said only 'returned non-zero exit status 1', which gave
    no clue what went wrong -- the tool's stderr was captured and discarded."""
    import subprocess

    from worker.app.pipeline import PipelineError, compress_glb

    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd, stderr=b"Error: invalid GLB header")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PipelineError, match="invalid GLB header"):
        compress_glb(tmp_path / "a.glb", tmp_path / "b.glb")


def test_compress_glb_reports_a_missing_binary(monkeypatch, tmp_path):
    import subprocess

    from worker.app.pipeline import PipelineError, compress_glb

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PipelineError, match="not found"):
        compress_glb(tmp_path / "a.glb", tmp_path / "b.glb")


def test_compress_glb_times_out_rather_than_hanging(monkeypatch, tmp_path):
    """It hung for five minutes with no bound before failing."""
    import subprocess

    from worker.app.pipeline import PipelineError, compress_glb

    def fake_run(cmd, **kwargs):
        assert kwargs.get("timeout"), "compress_glb must pass a timeout"
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(PipelineError, match="timed out"):
        compress_glb(tmp_path / "a.glb", tmp_path / "b.glb")


def test_compress_glb_does_not_shell_out_to_npx(monkeypatch, tmp_path):
    """No network dependency at job time."""
    import subprocess

    from worker.app.pipeline import compress_glb

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    compress_glb(tmp_path / "a.glb", tmp_path / "b.glb")

    assert "npx" not in seen["cmd"]
    assert seen["cmd"][0] == "gltf-transform"
    assert "meshopt" in seen["cmd"]
