# Pinned worker image for the Pixal3D/TRELLIS.2 pipeline.
# ComfyUI is vendored as a submodule at vendor/ComfyUI, pinned to the exact SHA
# proven working on the RTX 5060 Ti in PHASE1.md. Weights are NOT baked in --
# they're bind-mounted at runtime (see docker-compose.yml) so this image stays
# small and swaps hardware without a rebuild.
FROM python:3.12-slim-bookworm

# libgl1/libglib2.0-0: needed by OpenCV/PyOpenGL-adjacent deps pulled in
# transitively by ComfyUI's vision + 3D stack.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/ComfyUI

# Torch pinned to the exact build already verified against these weights on
# this GPU (PHASE1.md environment table) -- driver is CUDA 13.2-capable
# (Blackwell sm_120), so we install the matching cu130 wheels rather than
# PLAN.md's original cu128 guess.
RUN pip install --no-cache-dir \
    torch==2.12.0 torchvision==0.27.0 torchaudio==2.11.0 \
    --index-url https://download.pytorch.org/whl/cu130

COPY vendor/ComfyUI/requirements.txt /app/ComfyUI/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY vendor/ComfyUI /app/ComfyUI

# Model dirs are populated entirely by bind mounts at runtime -- see
# docker-compose.yml. Creating them here just avoids Docker auto-creating
# them as root-owned on first mount.
RUN mkdir -p \
    models/vae \
    models/background_removal \
    models/clip_vision \
    models/geometry_estimation \
    models/diffusion_models \
    input \
    output

EXPOSE 8188

ENTRYPOINT ["python", "main.py", "--listen", "0.0.0.0", "--port", "8188"]
