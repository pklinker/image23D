# Phase 4: merges what used to be two images -- the GPU ComfyUI server
# (docker/worker.Dockerfile) and the CPU-only ARQ orchestration worker
# (docker/arq-worker.Dockerfile) -- into one. ComfyEmbeddedPipeline
# (worker/app/embedded_pipeline.py) imports ComfyUI's own executor directly
# in-process, so the code that talks to ComfyUI and the code with GPU/CUDA
# access now have to live in the same process. docker/worker.Dockerfile is
# kept separately as an opt-in interactive dev server (see the "dev" profile
# in docker-compose.yml) for editing/exporting workflows through the actual
# ComfyUI UI -- it is not part of the default running stack anymore.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    ca-certificates \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/ComfyUI

# Torch pinned to the exact build verified against these weights on this GPU
# (PHASE1.md environment table).
RUN pip install --no-cache-dir \
    torch==2.12.0 torchvision==0.27.0 torchaudio==2.11.0 \
    --index-url https://download.pytorch.org/whl/cu130

COPY vendor/ComfyUI/requirements.txt /app/ComfyUI/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY vendor/ComfyUI /app/ComfyUI

RUN mkdir -p \
    models/vae \
    models/background_removal \
    models/clip_vision \
    models/geometry_estimation \
    models/diffusion_models \
    input \
    output

WORKDIR /app
COPY worker/requirements.txt /app/worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt

COPY common /app/common
COPY worker /app/worker

# /app so `common`/`worker` import as packages; /app/ComfyUI so ComfyUI's own
# bare-name internal imports (folder_paths, nodes, execution, cuda_malloc...)
# resolve exactly as they do when main.py runs them directly -- see
# PHASE4.md for why this works without any extra cli_args configuration
# (folder_paths.py resolves its default model/input/output dirs relative to
# its own file location, not CWD or argv).
ENV PYTHONPATH=/app:/app/ComfyUI

ENTRYPOINT ["arq", "worker.app.worker_settings.WorkerSettings"]
