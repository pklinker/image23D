"""ProgressTracker tests -- PLAN-BUGFIX.md items 2 and 3.

Deliberately free of ComfyUI, GPU and database: ProgressTracker must stay
importable without COMFY_ROOT on sys.path, so the event model can be tested
against a synthetic event sequence rather than a 70s GPU run.

The sequence used here is the real one for the pruned graph: `executing` per
node as it starts, `executed` only for the two SaveGLB nodes, and
`execution_success` at the end (with no terminal `executing {node: None}`,
which the embedded backend never receives).

Timing tests drive an explicit clock rather than sleeping, so stage attribution
is asserted exactly.
"""
import uuid
from pathlib import Path

import pytest

from worker.app.pipeline import STAGE_ORDER, ProgressTracker


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def tracker(clock):
    reports: list[tuple] = []
    artifacts: list[tuple[str, Path]] = []

    async def on_stage(stage, timings, total_seconds):
        reports.append((stage, timings, total_seconds))

    async def on_artifact(name, path):
        artifacts.append((name, path))

    t = ProgressTracker(Path("/out"), uuid.UUID(int=1), on_stage, on_artifact, clock=clock)
    t.reports = reports
    t.recorded_artifacts = artifacts
    return t


def _saveglb_output(filename, subfolder="jobs/x"):
    return {"3d": [{"filename": filename, "subfolder": subfolder, "type": "output"}]}


def _labels(tracker):
    return [stage for stage, _, _ in tracker.reports]


# --- item 3: stage attribution -------------------------------------------


async def test_duration_is_billed_to_the_stage_that_ran(tracker, clock):
    """The bug: durations were recorded against the *next* stage.

    Here segmentation runs 3s and the structure stage 10s. Before item 3 the
    3s would have been reported as structure_coarse_mesh's duration.
    """
    await tracker.handle_executing("193")   # segment_crop_fov starts
    clock.advance(3)
    await tracker.handle_executing("319")   # structure_coarse_mesh starts
    clock.advance(10)
    await tracker.finish()

    assert tracker.timings() == [
        {"stage": "segment_crop_fov", "seconds": 3.0},
        {"stage": "structure_coarse_mesh", "seconds": 10.0},
    ]


async def test_final_stage_is_recorded(tracker, clock):
    """The last stage's work was never recorded at all: nothing followed it to
    trigger the transition that closed it."""
    await tracker.handle_executing("252")   # remesh_paint_final starts
    clock.advance(10.5)
    await tracker.finish()

    assert tracker.timings() == [{"stage": "remesh_paint_final", "seconds": 10.5}]


async def test_label_is_current_not_lagging(tracker, clock):
    """The UI named the previous stage while the next one ran -- 'Segmenting'
    was displayed through the 11s Pixal3DConditioning."""
    await tracker.handle_executing("193")
    assert _labels(tracker) == ["segment_crop_fov"]

    await tracker.handle_executing("298")   # Pixal3DConditioning, 11s
    assert _labels(tracker) == ["segment_crop_fov", "structure_coarse_mesh"]


async def test_startup_time_is_billed_to_the_first_stage(tracker, clock):
    """Executor startup happens before the first `executing` event. Discarding
    it would make the per-stage seconds fail to add up to the total."""
    clock.advance(4)                        # startup, before any node runs
    await tracker.handle_executing("193")
    clock.advance(6)
    await tracker.finish()

    assert tracker.timings() == [{"stage": "segment_crop_fov", "seconds": 10.0}]
    assert sum(t["seconds"] for t in tracker.timings()) == pytest.approx(tracker.total_seconds())


async def test_durations_sum_to_total(tracker, clock):
    for node in ["193", "319", "91", "98", "252"]:
        await tracker.handle_executing(node)
        clock.advance(5)
    await tracker.finish()

    assert sum(t["seconds"] for t in tracker.timings()) == pytest.approx(25.0)
    assert tracker.total_seconds() == pytest.approx(25.0)


async def test_reentered_stage_accumulates(tracker, clock):
    """ComfyUI's ux_friendly_pick_node may schedule a later stage's node early
    (a loader, say), so a stage can be entered more than once. Durations must
    add up rather than the second visit overwriting the first."""
    await tracker.handle_executing("91")    # shape_upsample
    clock.advance(4)
    await tracker.handle_executing("193")   # segment node runs late
    clock.advance(1)
    await tracker.handle_executing("18")    # back to shape_upsample
    clock.advance(6)
    await tracker.finish()

    assert tracker.timings() == [
        {"stage": "segment_crop_fov", "seconds": 1.0},
        {"stage": "shape_upsample", "seconds": 10.0},
    ]


async def test_reported_label_only_moves_forward(tracker, clock):
    """Out-of-order scheduling must not make the UI jump backwards."""
    await tracker.handle_executing("91")    # shape_upsample
    await tracker.handle_executing("193")   # earlier stage, runs late
    await tracker.handle_executing("18")    # shape_upsample again

    assert _labels(tracker) == ["shape_upsample"]
    # ...but its time is still billed correctly (see previous test).


async def test_timings_are_reported_in_plan_order(tracker, clock):
    for node in ["252", "98", "91", "319", "193"]:  # reverse order
        await tracker.handle_executing(node)
        clock.advance(1)
    await tracker.finish()

    assert [t["stage"] for t in tracker.timings()] == STAGE_ORDER


async def test_unmapped_node_bills_to_the_open_stage(tracker, clock):
    await tracker.handle_executing("91")
    clock.advance(2)
    await tracker.handle_executing("99999")  # not in STAGE_MAP
    clock.advance(3)
    await tracker.finish()

    assert tracker.timings() == [{"stage": "shape_upsample", "seconds": 5.0}]


# --- item 2: event model --------------------------------------------------


async def test_stage_transitions_follow_node_order(tracker):
    for node in ["193", "319", "91", "98", "252"]:
        await tracker.handle_executing(node)
    await tracker.finish()

    assert _labels(tracker)[: len(STAGE_ORDER)] == STAGE_ORDER


async def test_each_stage_is_reported_once(tracker):
    for node in ["193", "192", "248", "312", "55", "56"]:
        await tracker.handle_executing(node)

    assert _labels(tracker) == ["segment_crop_fov"]


async def test_coarse_artifact_uses_the_reported_filename(tracker):
    """The path used to be hardcoded as coarse_00001_.glb. SaveGLB's counter
    comes from a directory scan, so it is only 1 for a fresh folder."""
    await tracker.handle_executing("1001")
    await tracker.handle_executed("1001", _saveglb_output("coarse_00007_.glb"))

    assert tracker.recorded_artifacts == [("coarse", Path("/out/jobs/x/coarse_00007_.glb"))]


async def test_artifact_fires_as_soon_as_the_node_reports(tracker):
    await tracker.handle_executing("1001")
    await tracker.handle_executed("1001", _saveglb_output("coarse_00001_.glb"))

    assert len(tracker.recorded_artifacts) == 1
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


async def test_terminal_executing_none_closes_the_run(tracker, clock):
    """The http backend's terminal signal, from main.py's prompt_worker."""
    await tracker.handle_executing("252")
    clock.advance(7)

    assert await tracker.handle_executing(None) is True
    assert tracker.timings() == [{"stage": "remesh_paint_final", "seconds": 7.0}]


async def test_full_run_sequence(tracker, clock):
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
            clock.advance(2)
        else:
            await tracker.handle_executed(node, _saveglb_output(f"{node}.glb"))
    await tracker.finish()  # execution_success

    assert [t["stage"] for t in tracker.timings()] == STAGE_ORDER
    assert [n for n, _ in tracker.recorded_artifacts] == ["coarse"]
    assert sum(t["seconds"] for t in tracker.timings()) == pytest.approx(tracker.total_seconds())
    assert all(t["seconds"] > 0 for t in tracker.timings())
