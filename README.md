# image23D

Web service that converts a single athlete action photo into a viewable,
vertex-colored 3D model (GLB).

Built on ComfyUI's Pixal3D / TRELLIS.2 core nodes, running on CUDA.

- **[PLAN.md](PLAN.md)** — architecture, pipeline pruning, and phased build plan
- **[usain-bolt.json](usain-bolt.json)** — reference ComfyUI workflow (UI format),
  the working pipeline this service is built from

## Status

Pre-Phase 1. Planning complete; environment bring-up not yet started.

## Hardware

Inference requires **Linux + CUDA** (Blackwell `sm_120`, CUDA 12.8+, PyTorch cu128).
Development target is an RTX 5060 Ti 16GB. macOS is a client/authoring environment
only — the int8 diffusion weights and sparse-voxel stages are CUDA-path code.
