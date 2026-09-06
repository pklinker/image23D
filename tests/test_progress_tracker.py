"""ProgressTracker tests -- PLAN-BUGFIX.md item 2.

Deliberately free of ComfyUI, GPU and database: ProgressTracker must stay
importable without COMFY_ROOT on sys.path, so the event model can be tested
against a synthetic event sequence rather than a 70s GPU run.

The sequence used here is the real one for the pruned graph: `executing` per
node as it starts, `executed` only for the two SaveGLB nodes, and
`execution_success` at the end (with no terminal `executing {node: None}`,
which the embedded backend never receives).
"""
import uuid
from pathlib import Path

import pytest

from worker.app.pipeline import ProgressTracker


@pytest.fixture
def tracker():
    stages: list[tuple[str, float]] = []
    artifacts: list[tuple[str, Path]] = []

    async def on_stage(stage, seconds):
        stages.append((stage, seconds))

    async def on_artifact(name, path):
        artifacts.append((name, path))

    t = ProgressTracker(Path("/out"), uuid.UUID(int=1), on_stage, on_artifact)
    t.recorded_stages = stages
    t.recorded_artifacts = artifacts
    return t


def _saveglb_output(filename, subfolder="jobs/x"):
    return {"3d": [{"filename": filename, "subfolder": subfolder, "type": "output"}]}


async def test_stage_transitions_follow_node_order(tracker):
    # One node from each of PLAN.md sec.6's five stages, in execution order.
    for node in ["193", "319", "91", "98", "252"]:
        await tracker.handle_executing(node)
    await tracker.finish()

    assert [s for s, _ in tracker.recorded_stages] == [
        "segment_crop_fov",
        "structure_coarse_mesh",
        "shape_upsample",
        "texture_sample",
        "remesh_paint_final",
    ]


async def test_each_stage_is_reported_once(tracker):
    for node in ["193", "192", "248", "312", "55", "56"]:
        await tracker.handle_executing(node)
    await tracker.finish()

    assert [s for s, _ in tracker.recorded_stages] == ["segment_crop_fov"]


async def test_coarse_artifact_uses_the_reported_filename(tracker):
    """The path used to be hardcoded as coarse_00001_.glb. SaveGLB's counter
    comes from a directory scan, so it is only 1 for a fresh folder."""
    await tracker.handle_executing("1001")
    await tracker.handle_executed("1001", _saveglb_output("coarse_00007_.glb"))

    assert tracker.recorded_artifacts == [("coarse", Path("/out/jobs/x/coarse_00007_.glb"))]


async def test_artifact_fires_before_the_next_node_starts(tracker):
    """`executed` is the earliest possible signal -- the file exists the moment
    the node returns, rather than when the following node starts."""
    await tracker.handle_executing("1001")
    await tracker.handle_executed("1001", _saveglb_output("coarse_00001_.glb"))

    assert len(tracker.recorded_artifacts) == 1  # already published
    await tracker.handle_executing("91")
    assert len(tracker.recorded_artifacts) == 1  # and not published twice


async def test_final_saveglb_is_not_published_as_an_artifact(tracker):
    """Node 1002's file is collected from the pipeline's return value instead."""
    await tracker.handle_executed("1002", _saveglb_output("final_00001_.glb"))
    assert tracker.recorded_artifacts == []


async def test_duplicate_executed_events_publish_once(tracker):
    await tracker.handle_executed("1001", _saveglb_output("coarse_00001_.glb"))
    await tracker.handle_executed("1001", _saveglb_output("coarse_00001_.glb"))
    assert len(tracker.recorded_artifacts) == 1


async def test_output_without_a_3d_entry_is_ignored(tracker):
    """A preview node's payload shape must not crash the run."""
    await tracker.handle_executed("1001", {"images": [{"filename": "x.png"}]})
    await tracker.handle_executed("1001", {})
    assert tracker.recorded_artifacts == []


async def test_terminal_executing_none_reports_the_last_node(tracker):
    """The http backend's terminal signal, from main.py's prompt_worker."""
    await tracker.handle_executing("252")
    assert tracker.recorded_stages == []  # not yet: 252 has only started

    assert await tracker.handle_executing(None) is True
    assert [s for s, _ in tracker.recorded_stages] == ["remesh_paint_final"]


async def test_full_run_sequence(tracker):
    """End to end over the real event shape, including the embedded backend's
    lack of a terminal `executing {node: None}`."""
    events = [
        ("executing", "122"), ("executing", "193"), ("executing", "192"),
        ("executing", "312"), ("executing", "56"), ("executing", "242"),
        ("executing", "319"), ("executing", "298"), ("executing", "3"),
        ("executing", "119"), ("executing", "4"), ("executing", "247"),
        ("executing", "1001"), ("executed", "1001"),
        ("executing", "91"), ("executing", "18"), ("executing", "94"),
        ("executing", "23"), ("executing", "92"),
        ("executing", "98"), ("executing", "12"), ("executing", "93"),
        ("executing", "202"), ("executing", "241"), ("executing", "186"),
        ("executing", "238"), ("executing", "252"), ("executing", "282"),
        ("executing", "1002"), ("executed", "1002"),
    ]
    for kind, node in events:
        if kind == "executing":
            await tracker.handle_executing(node)
        else:
            await tracker.handle_executed(node, _saveglb_output(f"{node}.glb"))
    await tracker.finish()  # execution_success

    assert [s for s, _ in tracker.recorded_stages] == [
        "segment_crop_fov",
        "structure_coarse_mesh",
        "shape_upsample",
        "texture_sample",
        "remesh_paint_final",
    ]
    assert [n for n, _ in tracker.recorded_artifacts] == ["coarse"]
    # Every stage duration is real elapsed time, never negative.
    assert all(seconds >= 0 for _, seconds in tracker.recorded_stages)
