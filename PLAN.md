# image23D — Athlete Photo → Textured 3D Model

Enterprise web service that turns a single athlete action photo into a downloadable,
viewable GLB with per-vertex color, so coaches and athletes can orbit a recognizable
3D capture of a moment.

## Decisions locked

| Question | Answer |
|---|---|
| Deliverable | **GLB only.** Vertex-colored mesh. No measurement. |
| Fidelity | Remesh for clean topology + `PaintMesh` vertex colors. No UV unwrap, no PBR bakes. |
| Infrastructure | On-prem RTX 5060 Ti (16GB) Linux box. No cloud spend. Stay cloud-portable. |
| Load | Low volume, multi-minute turnaround acceptable |

**Explicitly deferred:** joint angles / SMPL-X body fitting, and full PBR texture maps.
Both are documented as extension points in §9 — neither is designed out.

---

## 1. Source pipeline

`usain-bolt.json` is 66 nodes, **all `comfy-core`** (v0.17.0 / 0.21.1 / 0.33.0).
No custom node packs — the whole pipeline lives in the ComfyUI repo itself, which is
what makes §5 tractable. The active branch is Pixal3D (`Switch to Trellis2` = false);
TRELLIS.2 is wired as a dormant alternate sharing the same samplers.

Weights (Comfy-Org HuggingFace repos):

| File | Dir | Needed? |
|---|---|---|
| `pixal3d_int8_convrot.safetensors` | diffusion_models | yes |
| `trellis_2_shape_vae_bf16.safetensors` | vae | yes |
| `trellis_2_texture_vae_bf16.safetensors` | vae | yes |
| `dino_v3_L_naf_fp32.safetensors` | clip_vision | yes |
| `moge_2_vitl_normal_fp16.safetensors` | geometry_estimation | yes |
| `birefnet.safetensors` | background_removal | yes |
| `trellis_2_int8_convrot.safetensors` | diffusion_models | no — dormant branch |

Six of seven. Dropping the dormant TRELLIS.2 UNET is free VRAM headroom on a 16GB card.

---

## 2. The pruned graph

**Keep** (~30 functional nodes — all four KSamplers survive):

```
122 LoadImage
193 LoadBackgroundRemovalModel → 192 RemoveBackground → 248 Switch → 303 MaskPreview
312 ImageCropToMask (1024², pad 1.1) → 302 PreviewImage       [passthroughs vanish in code]
 55 LoadMoGeModel → 56 MoGeInference → 242 MoGeGeometryToFOV
 15 CLIPVisionLoader ─┬→ 298 Pixal3DConditioning (camera_angle_x ← MoGe)
319 UNETLoader (pixal3d)
    ├→ 199 CFGOverride → 125 RescaleCFG → 108 ModelSamplingSD3 ─┐
    └→ 279 CFGOverride → 126 RescaleCFG ──────────────┐         │
                                                      │         │
 87 EmptyTrellis2LatentStructure ─────────────────────┼→ 3 KSampler(12, cfg 7.5)
117 VAELoader (shape) → 119 VaeDecodeStructureTrellis2(32) → VOXEL
  4 VoxelToMesh → 247 MeshToFile3D              [optional fast coarse preview artifact]
 91 Trellis2ShapeStage ────→ 18 KSampler(20, cfg 7.5) ←┘
 94 Trellis2UpsampleStage(1536) → 23 KSampler ←────────┘
 92 VaeDecodeShapeTrellis ─┬→ 202 GetMeshInfo → 241 RemeshMesh(768, udf)
                           │     → 186 DecimateMesh → 238 MeshSmoothNormals ─┐
                           └→ (shape_subdivides) ─┐                          │
 98 Trellis2TextureStage → 12 KSampler(12, cfg 1) │                          │
118 VAELoader (texture) → 93 VaeDecodeTextureTrellis → VOXEL COLORS ─────────┤
                                                                             │
                                          252 PaintMesh ←────────────────────┘
                                              → 282 MeshToFile3D → GLB
```

**Delete or bypass:**

- Bake tail: `196` UnwrapMesh, `147` BakeTextureFromVoxel, `224` BakeNormalMapFromMesh,
  `233` BakeAmbientOcclusion, `261` RenderUVAtlas, `210` ApplyTextureToMesh,
  `260` MeshSmoothNormals, `285` MeshToFile3D, `288` PrimitiveInt, `322` Save3DAdvanced
- Preview-only: `164` `207` `208` `226` `235` `262` PreviewImage,
  `246` `323` Preview3DAdvanced
- Dormant branch: `40` UNETLoader(trellis2), `299` Trellis2Conditioning,
  `314` `315` `318` ComfySwitchNode, `316` PrimitiveBoolean

The cut is the **bake tail only**: xatlas UV unwrap, a 4096² texture bake, a 64-sample
AO bake and a 2048 normal bake. Everything upstream — all four samplers, the remesh,
the decimate — stays. This is the expensive half of the tail without being the
expensive half of the generation.

### Rewiring notes

- `252 PaintMesh` already reads `mesh ← 238 MeshSmoothNormals` and
  `voxel_colors ← 93 VaeDecodeTextureTrellis` in the original graph. No rewire needed —
  you're deleting the branch that ran *parallel* to it, not the one you want.
- Keep the `92 VaeDecodeShapeTrellis → 93` link supplying `shape_subdivides`.
  It's easy to miss because it crosses the graph.
- `288 PrimitiveInt` (texture resolution, 4096) fed only `UnwrapMesh` and
  `BakeTextureFromVoxel`. Both gone, so it goes too.

### The one real tradeoff of the vertex-color path

**Color resolution is vertex resolution.** With UV textures you can decimate hard and
keep a 4096² map. With vertex colors, every face you remove is color detail you lose —
so `DecimateMesh` is now a *visual quality* knob, not just a file-size knob.

700k faces is too heavy for a browser. Start around **200–300k** and tune by eye on a
real athlete photo, watching the face and kit specifically. If 300k still looks muddy,
that's the signal to revisit full PBR (§9).

---

## 3. Platform

**Worker: `linux/amd64` + CUDA only.** The `int8_convrot` weights and the sparse-voxel
TRELLIS stages are CUDA-path code. The Mac is a dev machine and client, not a runtime.

- RTX 5060 Ti is Blackwell `sm_120` → **CUDA 12.8+, PyTorch cu128 wheels.**
- Build the torch arch list to also cover `sm_89` (L4/L40S) so the same image runs on
  rented hardware later without a rebuild.
- Multi-arch at the service tier only: `docker buildx` the API image for arm64+amd64 so
  it runs on the Mac; the worker image stays single-arch.

**Cloud portability without cloud spend:** containerize the worker now, keep weights on
a mounted volume rather than baked into the image, keep all paths and device selection
in env vars. Renting a GPU later becomes a compose-file change.

---

## 4. Stack

| Layer | Choice | Notes |
|---|---|---|
| API | FastAPI + Uvicorn, Pydantic v2 | Async, free OpenAPI spec |
| Queue | Redis + ARQ | GPU jobs are long and serial; concurrency = 1 per GPU |
| DB | Postgres | Job records, params, per-stage timings, provenance |
| Objects | MinIO (S3 API) | Swaps to real S3 unchanged if you ever move |
| Worker | Pinned ComfyUI + PyTorch cu128 | See §5 |
| Post | `gltf-transform` or `gltfpack` | Draco/meshopt compression — see §7 |
| Viewer | React + react-three-fiber + drei | See §7 for the vertex-color gotchas |
| Auth | OIDC (Entra/Okta) humans, API keys services | Phase 4 |
| Deploy | Docker Compose + NVIDIA Container Toolkit | K8s only if you outgrow one box |
| Telemetry | OpenTelemetry + Prometheus + dcgm-exporter | Per-stage GPU time matters a lot |

Python end to end. Splitting the API into Go or Node adds a serialization hop and a
second deploy story for no gain.

---

## 5. ComfyUI → code

Define one interface and implement it twice:

```python
class Pipeline(Protocol):
    def run(self, image: Path, params: JobParams) -> Artifacts: ...
```

**Phase 1 — `ComfyHttpPipeline`.** Run ComfyUI headless with `--listen`, POST the
**API-format** workflow export to `/prompt`, subscribe to `/ws` for progress.
Note: the JSON in this repo is *UI format*. The API export is a separate menu item and
is what `/prompt` accepts. Zero reimplementation risk, ships in days.

**Phase 2 — `ComfyEmbeddedPipeline`.** Import ComfyUI as a library:
`from nodes import NODE_CLASS_MAPPINGS`, pull `Trellis2ShapeStage`,
`VaeDecodeTextureTrellis`, `RemeshMesh`, `PaintMesh` etc. and call them in topological
order. `ComfyUI-to-Python-Extension` generates a first draft from the API JSON;
hand-clean it into a typed module. Buys resident models across jobs, real per-stage
timing, and unit-testable stages.

Rejected: reimplementing on the upstream microsoft/TRELLIS repo. The Pixal3D int8
weights are Comfy-Org-specific and you'd rebuild the mesh tail from scratch.

**Pin ruthlessly.** ComfyUI as a git submodule at an exact SHA — the three different
`ver` values already present in this graph show how much the internals move.
SHA-256 verify all six weight files at worker startup.

**Drop UI passthroughs.** `302`/`303` are `PreviewImage`/`MaskPreview` nodes used as
graph-authoring convenience, not real operations. In code, wire directly.

---

## 6. Service shape

```
React viewer ──> FastAPI ──> Postgres (jobs)
                    │     └─> MinIO (presigned upload)
                    └─> Redis queue
                            │
                  GPU worker (concurrency = 1)
                    ├─ Stage 1  segment + crop + FOV estimate
                    ├─ Stage 2  structure sampler → coarse voxel mesh → GLB (early)
                    ├─ Stage 3  shape upsample (1536) → high-res mesh
                    ├─ Stage 4  texture sampler → voxel colors
                    └─ Stage 5  remesh → decimate → PaintMesh → compress → GLB (final)
                            │
                     MinIO artifacts + SSE progress
```

`POST /v1/jobs` → `202` + job id. `GET /v1/jobs/{id}` polls. SSE pushes stage
transitions. Async only — these are minutes-long jobs; don't pretend otherwise in the
API shape.

**Emit Stage 2's coarse mesh as its own artifact.** Node `4 VoxelToMesh → 247` gives you
a rough mesh far earlier than the final one. Showing it in the viewer while Stages 3–5
run turns a multi-minute wait into visible progress, and it's nearly free.

---

## 7. Delivering the GLB

Vertex-colored GLBs have three well-known ways to look broken. Handle all three:

1. **`COLOR_0` must be enabled in the material.** three.js renders the mesh flat grey
   unless `material.vertexColors = true`. The single most common "my colors vanished"
   cause.
2. **Color space.** glTF `COLOR_0` is linear. If `PaintMesh` emits sRGB values you get a
   washed-out model. Verify against the ComfyUI viewport render and convert if needed.
3. **Size.** ~250k faces with per-vertex color is a chunky download. Run
   `gltf-transform` (Draco or meshopt) as a post-step — both preserve vertex colors.
   Do this in the worker, store both raw and compressed.

---

## 8. Phasing

1. **Reproduce.** Pinned ComfyUI + CUDA 12.8 container on the 5060 Ti. Run the pruned
   API-format workflow headless. Confirm output matches the interactive run. Record
   per-stage timings and peak VRAM — every later decision depends on these numbers.
2. **Wrap.** FastAPI + Redis + Postgres + MinIO. `ComfyHttpPipeline`. Async job API,
   SSE progress, GLB download, compression post-step.
3. **Viewer.** react-three-fiber GLB viewer with orbit, the §7 fixes, and the coarse
   preview swap-in. Tune the decimate target here against real athlete photos.
4. **Harden.** OIDC, tenancy, audit log, retention policy, rate limits. Swap in
   `ComfyEmbeddedPipeline`.

---

## 9. Deferred, not designed out

Both of these were in scope earlier and were cut deliberately. Neither is blocked by
anything above.

**Full PBR textures.** Every node is still in `usain-bolt.json`: `196 UnwrapMesh`,
`147 BakeTextureFromVoxel`, `224 BakeNormalMapFromMesh`, `233 BakeAmbientOcclusion`,
`210 ApplyTextureToMesh`. Restoring them is a graph edit, not new engineering. The
trigger to revisit: if §2's decimate tuning shows vertex colors can't hold facial
detail at a web-viable face count. Expose it as a per-job `quality` tier when you do.

**Joint angles.** Would need a parametric body model (SMPL-X) rather than the mesh —
a triangle surface has no joint centers, and the Pixal3D output is in a normalized cube
with no metric scale. Requires an estimator bake-off (NLF / CameraHMR / HMR2.0, minding
their commercial licence terms), a scale anchor from athlete height, ICP registration
against the mesh, and ISB joint-coordinate-system conventions for the angles to mean
anything to a sports scientist. Keep the camera intrinsics from `242 MoGeGeometryToFOV`
in the job record — that's the one thing that would be annoying to recover later.

---

## Open items

- Decimate target vs. visible color fidelity — tune in Phase 3 on real photos
- Draco vs. meshopt for the compression step
- Whether the coarse Stage 2 preview is worth wiring into the viewer or just logged
- Whether multi-photo input is on the roadmap; it would materially improve fidelity
