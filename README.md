# image23D

Turns a **single photograph of a person in motion into a textured 3D model** you can
orbit in a browser or download as a GLB.

Upload a still, get back a vertex-coloured mesh of the moment — the pose, the kit, the
face — reconstructed from that one frame. It runs as a service: a REST API, an async job
queue backed by a local GPU, object storage for the artifacts, and a small React viewer.

Built on ComfyUI's Pixal3D / TRELLIS.2 core nodes. Everything runs on your own hardware;
nothing is sent to a third party.

```
photo ──> POST /v1/uploads ──> PUT to object storage
      ──> POST /v1/jobs     ──> queued on the GPU worker
      ──> GET  /v1/jobs/{id}/events   (Server-Sent Events, live progress)
      ──> coarse preview mesh at ~20s, final textured GLB at ~50s
```

## How a job runs

One photo takes **50–60 seconds** on an RTX 5060 Ti, in five reported stages:

| Stage | What happens | Typical |
|---|---|---|
| `segment_crop_fov` | Background removal, crop to subject, camera FOV estimate | ~3s |
| `structure_coarse_mesh` | Image conditioning + structure sampler → coarse voxel mesh | ~18s |
| `shape_upsample` | Shape sampler, upsampled to 1536 | ~16s |
| `texture_sample` | Texture sampler → voxel colours | ~9s |
| `remesh_paint_final` | Remesh, decimate, paint vertex colours, export | ~5s |

The **coarse mesh is published as its own artifact partway through**, so the viewer has
something to show while the rest finishes rather than a blank canvas for a minute.

Three artifacts land in object storage per job: the coarse preview, the final GLB
(~7.5 MB), and a meshopt-compressed final (~1.25 MB, same geometry and colours).

## Requirements

- **Linux host with an NVIDIA GPU.** The int8 diffusion weights and sparse-voxel stages
  are CUDA-path code — there is no CPU or Apple-silicon fallback. Peak usage measured at
  ~12.7 GB, so a **16 GB card** is the practical floor.
- **Docker Engine + the NVIDIA Container Toolkit.** `scripts/install-docker-nvidia.sh`
  installs both on Ubuntu/Debian-family hosts.
- **Model weights** (~6 files, several GB) downloaded to the host. They are deliberately
  *not* baked into the image, so moving to different hardware is a config change.

Built and verified against PyTorch 2.12.0 + cu130, on a CUDA 13.2 driver.

## Setup

### 1. Clone with the pinned ComfyUI

ComfyUI is vendored as a submodule at an exact commit — its internals move quickly and
the graph depends on specific node behaviour.

```bash
git clone --recurse-submodules https://github.com/pklinker/image23D.git
cd image23D
# already cloned without --recurse-submodules?
git submodule update --init --recursive
```

### 2. Fetch the model weights

Six files, from the Comfy-Org repositories on Hugging Face. Put them in directories
laid out the way ComfyUI expects:

| File | Directory |
|---|---|
| `pixal3d_int8_convrot.safetensors` | `diffusion_models/` |
| `trellis_2_shape_vae_bf16.safetensors` | `vae/` |
| `trellis_2_texture_vae_bf16.safetensors` | `vae/` |
| `dino_v3_L_naf_fp32.safetensors` | `clip_vision/` |
| `moge_2_vitl_normal_fp16.safetensors` | `geometry_estimation/` |
| `birefnet.safetensors` | `background_removal/` |

These are bind-mounted read-only into the worker; the paths are set in `.env` below and
may point anywhere on the host, including an existing ComfyUI install.

### 3. Configure

```bash
cp .env.example .env
```

Then edit `.env`:

- **`COMFY_MODELS_*`** — absolute host paths to the six weight directories above.
- **`REDIS_PASSWORD`** and **`REDIS_URL`** — generate one and use the same value in both.
  Redis is the job queue, so an unauthenticated Redis is an unauthenticated way to run
  GPU jobs:
  ```bash
  openssl rand -base64 24
  ```
- **`POSTGRES_PASSWORD`**, **`S3_SECRET_KEY`** — change from the example values.

Compose refuses to start if any credential is missing, rather than quietly falling back
to a well-known default.

### 4. Start it

```bash
docker compose up -d --wait
```

`--wait` blocks until every service reports healthy (about 15 seconds from cold). The API
container runs database migrations before serving, and the worker waits for the API, so
there is no start-up ordering to manage by hand.

| Service | Address | Notes |
|---|---|---|
| Viewer | http://localhost:5173 | Upload UI and 3D viewer |
| API | http://localhost:8000 | OpenAPI docs at `/docs` |
| MinIO console | http://localhost:9001 | Object storage admin |

Postgres, Redis and MinIO bind to `127.0.0.1` only. The API and viewer are the sole
services reachable from off the box.

### 5. Create the first API key

Every `/v1` route requires a key, and there is no unauthenticated way to mint one — so
the first is written straight to the database:

```bash
docker compose exec api python scripts/create_api_key.py --name "my-key"
```

It prints the key once; it is stored only as a hash and cannot be recovered. Paste it
into the viewer when prompted, or send it as `Authorization: Bearer <key>`.

Keys carry a scope. `--scope admin` (the default for this bootstrap path) can create and
revoke other keys; `service` can only run jobs, and is what you should hand to an
integration.

## Using it

### From the browser

Open http://localhost:5173, paste your API key, choose a photo. You will see live
progress, a coarse preview mesh partway through, and the final textured model with orbit
controls.

### From the API

```bash
export KEY=i23d_...

# 1. ask for an upload URL
curl -s -X POST http://localhost:8000/v1/uploads \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"filename":"athlete.png","content_type":"image/png"}'

# 2. PUT the image straight to the returned upload_url (no API hop)
curl -X PUT "<upload_url>" -H "Content-Type: image/png" --data-binary @athlete.png

# 3. start the job
curl -s -X POST http://localhost:8000/v1/jobs \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"object_key":"<object_key>"}'

# 4. follow progress (Server-Sent Events; each frame is a full job status)
curl -N "http://localhost:8000/v1/jobs/<job_id>/events?api_key=$KEY"
```

Or run the whole thing end to end:

```bash
IMAGE23D_API_KEY=$KEY python3 scripts/e2e_smoke_test.py path/to/photo.png
```

### Routes

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/uploads` | Presigned upload URL |
| `POST` | `/v1/jobs` | Start a job → `202` + job id |
| `GET` | `/v1/jobs/{id}` | Poll status, timings, artifact URLs |
| `GET` | `/v1/jobs/{id}/events` | SSE progress stream |
| `POST` | `/v1/api-keys` | Mint a key (admin only) |
| `GET` | `/v1/api-keys` | List keys (admin only) |
| `POST` | `/v1/api-keys/{id}/revoke` | Revoke a key (admin only) |
| `GET` | `/healthz` | Liveness, unauthenticated |

### Job parameters

All optional — omit `params` entirely for defaults.

| Parameter | Default | Meaning |
|---|---|---|
| `bbox` | none | `[x0, y0, x1, y1]` normalised 0–1, saying where the subject is. Omit and the whole frame is segmented, which cannot tell one person from another — anyone else in shot is merged into the same subject. Supply it and the image is cropped first. |
| `target_face_count` | `250000` | Decimation target, 50k–700k. With vertex colours this is a *visual quality* knob, not just a file-size one: every face removed is colour detail lost. |
| `seed` | none | Applied to all four samplers. Omit to keep the stock seeds, which is the reproducible default. |

```json
{"object_key": "uploads/...", "params": {"bbox": [0.30, 0.02, 0.75, 0.96]}}
```

Unknown parameters and malformed values are rejected with `422` rather than silently
ignored, so a mistake surfaces immediately instead of producing a plausible-looking model
built from the wrong region.

## Operations

```bash
docker compose logs -f worker      # GPU pipeline
docker compose ps                  # health
docker compose down                # stop (volumes persist)
```

- **Concurrency is one job at a time**, matching the single GPU. Additional jobs queue.
- **Retention**: jobs and their artifacts are purged after `RETENTION_DAYS` (default 30).
  Abandoned uploads expire on the same window via an object-storage lifecycle rule.
- **Rate limits**: per key, on job creation and uploads. See `.env`.
- Scratch files are cleaned up per job, and any left behind by a previous worker are
  swept at start-up.

## Development

Tests run against a real Postgres and Redis, using an isolated database and Redis index
so nothing touches live data. Credentials are read from `.env`.

```bash
python3 -m venv .venv-dev
.venv-dev/bin/pip install -r requirements-dev.txt
PYTHONPATH=. .venv-dev/bin/python -m pytest
```

Most of the suite is pure logic and needs no infrastructure at all:

```bash
PYTHONPATH=. .venv-dev/bin/python -m pytest tests/test_progress_tracker.py \
    tests/test_cleanup.py tests/test_job_params.py
```

There is also a real-browser check that drives the viewer end to end and asserts each
mesh is downloaded exactly once:

```bash
.venv-dev/bin/python -m playwright install chromium-headless-shell
IMAGE23D_API_KEY=$KEY .venv-dev/bin/python scripts/browser_check.py
```

### Layout

```
api/          FastAPI service — routes, auth, rate limiting
worker/       ARQ worker — the GPU pipeline and job orchestration
common/       Shared models, schemas, settings, storage
viewer/       React + react-three-fiber viewer
alembic/      Database migrations
scripts/      Bootstrap, smoke tests, workflow pruning
vendor/       ComfyUI, pinned as a submodule
```

The pipeline is defined by `usain-bolt.pruned.api.json`, generated from the reference
workflow by `scripts/prune_workflow.py`. It runs ComfyUI's executor **in-process** rather
than over HTTP, so node logic is never reimplemented.

## Limitations

Worth being clear about, because they are properties of single-image reconstruction
rather than bugs:

- **The unseen side is invented.** The far arm, the back, occluded limbs and the back of
  the head are inferred from priors, not observed. Orbit behind the model and detail such
  as a number on a shirt will be plausible but wrong. Treat the output as a 3D *capture of
  a moment*, not a reconstruction.
- **It is not measurement.** There is no metric scale, no joint centres, no angles. The
  model sits in a normalised cube.
- **Frames are independent.** Two adjacent video frames produce two different
  reconstructions; there is no temporal consistency, so this does not make 3D video.
- **One subject per photo.** Without a `bbox`, background removal separates foreground
  from background, not one person from another.
- **Input quality dominates.** Motion blur, heavy compression, or a subject only a few
  hundred pixels tall all degrade the result noticeably.

## Further reading

- **[PLAN.md](PLAN.md)** — architecture, pipeline pruning, and the phased build plan
- **[PHASE1.md](PHASE1.md)**–**[PHASE4.md](PHASE4.md)** — what was built at each phase,
  with measured timings, VRAM, and the problems found along the way
- **[usain-bolt.json](usain-bolt.json)** — the reference ComfyUI workflow this is built from
