# Phase 2 — Wrap

Status per PLAN.md §8.2: **done.** FastAPI + Redis/ARQ + Postgres + MinIO around the
Phase 1 container, verified end to end with a real upload → job → GLB round trip.

## Architecture

```
client -> POST /v1/uploads          -> presigned MinIO PUT url
client -> PUT <presigned url>       -> image lands in MinIO directly (no API hop)
client -> POST /v1/jobs {object_key} -> Postgres row (pending) + ARQ enqueue, 202
client -> GET  /v1/jobs/{id}         -> poll status/stage/timings/artifact urls
client -> GET  /v1/jobs/{id}/events  -> SSE stream of stage transitions

ARQ worker (CPU only, no torch) on job pickup:
  1. download input from MinIO -> shared "comfy_input" volume, named "{job_id}.<ext>"
  2. patch the pruned graph's LoadImage filename + SaveGLB filename_prefixes
  3. POST /prompt to comfy-worker, subscribe /ws, map node id -> one of PLAN.md sec.6's
     five stages, publish each transition to Redis pub/sub + persist to Postgres
  4. on completion, read GLBs from shared "comfy_output" volume
  5. gltf-transform meshopt compress the final GLB
  6. upload coarse/final/compressed to MinIO, mark job succeeded
```

`api/` and `worker` are separate containers/images; `worker` has no GPU and no
ComfyUI/torch dependency at all — it only speaks HTTP/WS to `comfy-worker`
(the Phase 1 container), matching PLAN.md's `ComfyHttpPipeline` design (§5) exactly:
zero reimplementation of node logic, this phase is pure orchestration.

Code layout:

- `common/` — shared between `api` and `worker`: SQLAlchemy `Job` model, Pydantic
  schemas, settings (`pydantic-settings`, reads `.env`), S3/MinIO client wrapper.
- `api/app/main.py` — the four routes above.
- `worker/app/pipeline.py` — `ComfyHttpPipeline`, `STAGE_MAP` (node id → stage),
  `compress_glb`.
- `worker/app/tasks.py` — `run_pipeline_job`, the ARQ task: DB/Redis bookkeeping around
  the pipeline call.
- `docker/api.Dockerfile`, `docker/arq-worker.Dockerfile` — both `python:3.12-slim`,
  no CUDA. The arq-worker image also installs Node 22 for `npx @gltf-transform/cli`.

## Why input/output moved from host bind-mounts to named Docker volumes

`LoadImage` reads by filename from ComfyUI's own local `input/` directory — it can't
take an arbitrary path. Phase 1's `comfy-worker` bind-mounted `input`/`output` straight
to a host directory for manual testing convenience. Now that a second container (the
ARQ worker) needs to write images in and read GLBs out of that same filesystem, host
paths would only work if both containers ran on the same physical host forever, which
contradicts PLAN.md §3's "cloud-portable" requirement. Both mounts became named volumes
(`comfy_input`, `comfy_output`) shared between `comfy-worker` and `worker`; the model
weight mounts stay host bind-mounts since they're huge, external, read-only assets, not
inter-container plumbing.

## Design choices worth flagging

- **No automatic retry on pipeline failure.** `run_pipeline_job` catches every
  exception itself and records `status=failed` + `error` rather than letting it
  propagate to ARQ. A GPU job here costs ~60s of exclusive GPU time; blindly retrying a
  deterministically-broken input would just burn that twice for the same result. ARQ's
  own retry mechanism is still available for infrastructure-level failures if this turns
  out to be too conservative in practice.
- **Schema via `Base.metadata.create_all()` at API startup, not Alembic.** One table,
  pre-production, still-moving schema — a migration tool earns its keep once the schema
  stabilizes or a second environment needs to track drift. Worth adding before Phase 4.
- **`S3_ENDPOINT_URL` vs `S3_PUBLIC_ENDPOINT_URL`.** Presigned URLs are signed for and
  handed to a browser outside the compose network, so they must point at
  `localhost:9000`, not the internal `minio:9000` the API/worker use for their own S3
  calls. Getting this backwards produces URLs that resolve DNS-wise nowhere useful
  outside the compose network — worth remembering if MinIO ever moves behind a real
  reverse proxy.
- **Concurrency = 1**, enforced via ARQ `max_jobs = 1` on the worker (PLAN.md §4)
  matching the single-GPU constraint, not a queue-depth choice.

## Verified (real run against the RTX 5060 Ti, `usain-bolt.json` test image)

```
upload -> job 7dae1fc7... -> 72.1s wall clock -> succeeded
  segment_crop_fov         0.02s
  structure_coarse_mesh    0.63s
  shape_upsample          22.30s
  texture_sample          21.94s
  remesh_paint_final      10.66s
```

- All three artifacts (`coarse.glb`, `final.glb`, `final.compressed.glb`) landed in
  MinIO with working presigned GET URLs.
- Compression: **20,998,228 → 3,216,416 bytes (85% reduction)**, `EXT_meshopt_compression`
  + `KHR_mesh_quantization` present, `COLOR_0` (vertex colors) confirmed intact after
  compression — meshopt does not touch color data, per PLAN.md §7.3's assumption.
- SSE (`GET /v1/jobs/{id}/events`) streams every stage transition in real time and
  closes cleanly on `succeeded`/`failed`, verified against a live second job while the
  worker's `max_jobs=1` queued it behind the first.

Stage timings here are per-job-loop totals (time between distinct stage entries seen on
the websocket), not the same as PHASE1.md's per-node breakdown — both are consistent
with each other (e.g. `shape_upsample` here ≈ nodes 91/18/94/23/92 combined in
PHASE1.md's table).

## Not yet done

- Draco vs. meshopt comparison (PLAN.md open item) — only meshopt implemented so far.
- Auth (OIDC/API keys) — explicitly Phase 4.
- Load/retry/backpressure behavior beyond the single-GPU-queue-of-one case above.
- react-three-fiber viewer (Phase 3).
