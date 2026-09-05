# Phase 1 — Reproduce

Status per PLAN.md §8.1: **done.** The pruned graph runs headless against ComfyUI's
`/prompt` API and produces a vertex-colored GLB, on the target hardware, within budget.

## Environment

| | |
|---|---|
| Host | RTX 5060 Ti 16GB, driver 595.84, CUDA 13.2 |
| ComfyUI | `250b2e9551a7bc7a8ebb5beb07e0fecd2983e04a` (v0.34.0-56-g250b2e95, released 2026-09-04) |
| PyTorch | `2.12.0+cu130` |
| Python | 3.12.3 |

Weights are split across two host paths (both are real, neither is a mistake to fix):

- `~/comfyui/ComfyUI/models/{vae,geometry_estimation,background_removal,clip_vision}` —
  `trellis_2_shape_vae_bf16`, `trellis_2_texture_vae_bf16`, `moge_2_vitl_normal_fp16`,
  `birefnet`, `dino_v3_L_naf_fp32`
- `/apps/comfyui/models/diffusion_models` — `pixal3d_int8_convrot`,
  `trellis_2_int8_convrot` (dormant, unused)

## What was built

- `usain-bolt.api.json` — API-format export of `usain-bolt.json`, done manually via the
  ComfyUI web UI (Workflow → Export API). This step is inherently manual; the UI-format
  → API-format transform lives in ComfyUI's frontend JS, not anywhere scriptable
  server-side.
- `scripts/prune_workflow.py` — deletes PLAN.md §2's bake tail (10 nodes), preview-only
  nodes (8), and the dormant TRELLIS.2 branch (6), 63 → 39 nodes, matching §2's kept-node
  diagram exactly (verified by set comparison, not eyeballed).
- `scripts/timed_run.py` — submits the pruned graph, records per-node wall time from the
  `/ws` executing-event stream.

## Two corrections to PLAN.md's §2 pruning, found by actually running it

1. **The dormant-branch switches (314/315/318) are not dead ends.** They sit between the
   live Pixal3D sources and real consumers (KSamplers 3/12/91, CFGOverrides 199/279),
   selected by `316 PrimitiveBoolean = False`. Deleting them without rewiring left 7
   dangling references. Fix: point consumers directly at the `on_false` targets (`298`
   Pixal3DConditioning outputs 0/1, `319` UNETLoader) and drop the switches. PLAN.md's
   claim that "no rewire needed" was true only for `252 PaintMesh`; it doesn't generalize
   to the whole dormant branch.

2. **`247`/`282 MeshToFile3D` do not save anything and are not `OUTPUT_NODE`s.** They
   build an in-memory `File3D` object; the actual disk write and `is_output_node=True`
   flag live on `322 Save3DAdvanced`, which §2 deletes as part of the bake tail. Without
   a replacement, ComfyUI's executor correctly prunes 247/282 and everything upstream of
   them as unreachable — confirmed by a first run that returned in 1.95s having executed
   only the background-removal branch. Fix: add a `SaveGLB` node
   (`is_output_node=True`, accepts `Mesh` or `File3D` directly, no rewiring needed on its
   input side) after each of 247 and 282. Two were added — one for §6's coarse Stage 2
   preview, one for the final GLB.

Net: the pruned graph is 41 API nodes (39 kept + 2 `SaveGLB`), not 39.

## Run result (cold cache, no reused nodes)

Total wall time: **64.05s**. Peak VRAM: **12,725 MiB** of 16,384 MiB.

| node | class_type | seconds |
|---|---|---|
| 298 | Pixal3DConditioning | 11.18 |
| 3 | KSampler (structure, 12 steps) | 7.61 |
| 18 | KSampler (shape, 20 steps) | 4.85 |
| 23 | KSampler (upsample) | 15.10 |
| 12 | KSampler (texture, 12 steps) | 9.04 |
| 241 | RemeshMesh | 3.28 |
| 186 | DecimateMesh | 2.05 |
| 238 | MeshSmoothNormals | 0.94 |
| 252 | PaintMesh | 0.49 |
| (all others) | — | < 2s combined |

Full per-node table: `/tmp/timed_run_result.json` (not checked in — regenerate with
`scripts/timed_run.py` against a freshly-restarted ComfyUI to keep the cache-free
timing honest).

## Output artifacts

- `output/3d/coarse_00001_.glb` — 89 KB, Stage 2 fast preview (§6)
- `output/3d/final_00001_.glb` — 21 MB, 350,062 vertices / 699,512 faces,
  `POSITION` + `NORMAL` + `COLOR_0` present (vertex colors confirmed on the wire, not
  just assumed)

**Open item carried to Phase 3, not a Phase 1 blocker:** 699,512 faces is right at
PLAN.md §2's "700k is too heavy for a browser" line — the stock `RemeshMesh` target
(768) needs tuning down toward 200–300k against real athlete photos, per the plan's own
call.

## Containerization

- `vendor/ComfyUI` — git submodule, pinned to `250b2e9` (same SHA verified above).
- `docker/worker.Dockerfile` — `python:3.12-slim-bookworm` base, not an `nvidia/cuda`
  base image: the `cu130` PyTorch wheels bundle their own CUDA runtime libs, and the
  NVIDIA Container Toolkit passes through the host driver's `libcuda.so`, so a full CUDA
  toolkit in the image buys nothing here. Model dirs are created empty in the image and
  populated entirely by bind mounts — nothing weight-related is baked in.
- `docker-compose.yml` + `.env` (`.env.example` checked in) — five read-only mounts for
  the split weight locations (§ above) plus input/output volumes, GPU passed through via
  `deploy.resources.reservations.devices`. Host paths live in `.env`, not the compose
  file, per PLAN.md §3's "keep paths in env vars" instruction.
- Verified: `docker compose build` succeeds, container starts, GPU is visible inside
  (`pytorch version: 2.12.0+cu130`, `Device: cuda:0 NVIDIA GeForce RTX 5060 Ti`), and all
  five mounted weight directories resolve.

**Containerized run vs. bare-metal run, same input, same pruned graph:**

| | bare metal | container |
|---|---|---|
| Total wall time | 64.05s | 62.12s |
| Peak VRAM | 12,725 MiB | 12,885 MiB |
| Final GLB vertices/faces | 350,062 / 699,512 | 350,034 / 699,484 |

Per-node timings match within noise (e.g. node 23 KSampler: 15.10s vs 14.54s). The
~0.01% difference in final vertex/face count is consistent with GPU float
non-associativity in `RemeshMesh`/`DecimateMesh` across runs, not a pipeline
discrepancy — same fixed seeds (56/43/42), same diffusion output. Containerizing adds no
measurable overhead.

## Not yet done

- Visual/quality comparison against an interactive run of the *same pruned subset*
  through the ComfyUI GUI (the numeric checks above — correct attributes, sane
  vertex/face counts, no execution errors, cross-checked bare-metal vs. container — stand
  in for that so far, but nobody has eyeballed the render).
- Phase 2 (§8): FastAPI + Redis + Postgres + MinIO wrapper around this container.
