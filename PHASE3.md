# Phase 3 — Viewer

Status per PLAN.md §8.3: **done.** react-three-fiber viewer with orbit, the §7
vertex-color fixes, and the coarse-preview swap-in — verified in a real headless
Chromium session against the fully containerized stack, not just unit-level checks.

## Two Phase 2 gaps fixed as part of building this

Building the viewer surfaced two problems in the Phase 2 wiring that weren't visible
until something actually consumed the artifacts end to end:

1. **The coarse preview wasn't actually early.** `PipelineError` aside, Phase 2's
   `tasks.py` uploaded `coarse_glb_key` and `final_glb_key` together, in the same commit,
   only after `pipeline.run()` returned — i.e. after the *entire* ~60-70s pipeline
   finished. The coarse mesh (PLAN.md §6) was being generated early on disk by ComfyUI
   but never surfaced early to the client, defeating the entire point of emitting it as
   a separate artifact. Fixed in `worker/app/pipeline.py`: `_stream_progress` now detects
   when node `1001` (the coarse `SaveGLB`) finishes — by noticing the *next* node has
   started, since ComfyUI's executor runs nodes strictly sequentially, so seeing the next
   node begin means `1001`'s synchronous file write already completed — and fires a new
   `on_artifact` callback. `tasks.py` uses it to upload and persist `coarse_glb_key`
   immediately. Verified: coarse GLB now appears at ~22s into a ~68s run, not at the end.

2. **Decimate target was still the stock 700k**, which PLAN.md's own §2 calls "too heavy
   for a browser." Tuned `186 DecimateMesh`'s `target_face_count` to 250,000 (both in the
   committed pruned graph and in `scripts/prune_workflow.py` so re-running the pruning
   script from a fresh export doesn't silently revert it). Re-verified: final mesh is now
   125,329 vertices / 249,727 faces, landing right in PLAN.md's target range, vertex
   colors still intact.

## Viewer

`viewer/` — Vite + React + TypeScript + `@react-three/fiber` + `@react-three/drei`.

- `src/api.ts` — upload/job/SSE fetch wrappers against the Phase 2 API.
- `src/Model.tsx` — loads a GLB via `useGLTF`, defensively forces
  `material.vertexColors = true` on every mesh material (PLAN.md §7.1: GLTFLoader
  usually sets this on its own when it sees `COLOR_0`, but it's the single most common
  "colors vanished" cause, so it costs nothing to double up on).
- `src/Viewer.tsx` — `<Canvas>` + `<OrbitControls>` + drei's `<Stage>` for auto-framing;
  explicitly sets `gl.outputColorSpace = SRGBColorSpace` (PLAN.md §7.2).
- `src/App.tsx` — upload flow, SSE-driven progress display, and the coarse→final swap:
  `modelUrl = final_glb_compressed_url ?? final_glb_url ?? coarse_glb_url`, so the viewer
  shows whatever's furthest along.

## A real bug found only by driving it in an actual browser

Initial version keyed `<Canvas key={url}>` to force a clean remount on every mesh swap.
`_job_to_status` re-signs presigned URLs (fresh signature + timestamp) on *every* GET,
even when the underlying object hasn't changed — so with the frontend refetching job
status on every SSE tick, `modelUrl`'s string value changed on almost every poll, not
just on real artifact transitions. That meant the WebGL context was being torn down and
recreated several times per job, visible in devtools as repeated
`THREE.WebGLRenderer: Context Lost`. This would never have shown up in a code read —
only in an actual run against real polling cadence.

Fix: dropped `key={url}` entirely. `useGLTF(url)`'s own Suspense-based loading swaps the
scene in without touching the Canvas or its WebGL context; drei's `<Stage>` still
re-fits the camera correctly on scene changes (confirmed — see verification below), so
no framing regression from removing the remount.

## CORS (two separate surfaces, not one)

The API's own routes (`fastapi.middleware.cors.CORSMiddleware`) and MinIO
(`MINIO_API_CORS_ALLOW_ORIGIN` env var) both needed configuring, because the browser
talks to *both* origins directly — presigned PUT/GET goes straight to MinIO, everything
else goes through the API. First attempt tried `boto3.put_bucket_cors()`, which MinIO
rejects (`NotImplemented`) — it dropped per-bucket S3 CORS in favor of the server-wide
env var.

## Containerized

`docker/viewer.Dockerfile` — multi-stage Node build → static `nginx:alpine` serve.
`VITE_API_BASE_URL` is a build `ARG`, not a runtime env var like every other service
here, because Vite bakes `VITE_*` vars into the JS bundle at build time — changing the
API origin means rebuilding this one image. Not worth solving with a runtime
`config.js` shim or a reverse proxy until there's an actual second environment to
support.

## Verified (headless Chromium via Playwright, driving the real containerized stack)

Uploaded the same test photo through the actual file input, not the API directly:

- **Vertex colors render correctly** — final mesh shows the runner's yellow/green
  Jamaica jersey, teal shorts, race bib ("3168"), skin tone, and shoes. Not flat grey
  (confirms the `vertexColors` fix works), not washed out or oversaturated (confirms no
  color-space conversion was needed — `PaintMesh`'s output already agrees with three.js's
  linear `COLOR_0` assumption).
- **Coarse preview renders too, correctly dark/uncolored** — the coarse mesh is a blocky
  voxel silhouette in a recognizable sprinting pose, rendered with no vertex colors,
  which is correct: `PaintMesh` hasn't run yet at that stage, only `VoxelToMesh` has.
- **Coarse→final swap happens live** — confirmed by screenshotting mid-run vs.
  post-completion.
- **Orbit controls work** — a drag gesture rotated the model to a 3/4 profile view that
  remained coherent and correctly proportioned from the new angle, not just a flat
  cutout facing one direction.
- **No CORS, 404, or WebGL errors** in the console after the two fixes above; only
  benign `THREE.Clock` deprecation warnings and GPU-stall notices caused by the test
  script's own `readPixels` instrumentation (not the app).

## Not yet done

- Draco vs. meshopt comparison (still open from Phase 2).
- Only one real test photo available (a Britannica article screenshot of Usain Bolt,
  not a clean isolated action shot) — decimate tuning above is based on that single
  image; PLAN.md's instruction to "tune by eye on a real athlete photo" deserves more
  than one data point before calling 250k final.
- Auth, tenancy, rate limits — Phase 4.
