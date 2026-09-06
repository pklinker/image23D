import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

# Formats Pillow reads reliably and the pipeline has been exercised on.
ALLOWED_CONTENT_TYPES = ("image/png", "image/jpeg", "image/webp")

# The graph's stock seeds (KSamplers 3/18/23/12). Left untouched unless a job
# asks for a specific seed, so the default run stays bit-for-bit reproducible.
SAMPLER_NODES = ("3", "18", "23", "12")

MIN_FACE_COUNT = 50_000
MAX_FACE_COUNT = 700_000
DEFAULT_FACE_COUNT = 250_000


class UploadRequest(BaseModel):
    filename: str
    content_type: Literal["image/png", "image/jpeg", "image/webp"] = "image/png"


class UploadResponse(BaseModel):
    object_key: str
    upload_url: str


class JobParams(BaseModel):
    """Per-job knobs. Unknown fields are rejected rather than silently ignored:
    a caller who misspells one should hear about it immediately, not discover
    later that their setting never applied."""

    model_config = {"extra": "forbid"}

    seed: int | None = Field(
        None,
        ge=0,
        description=(
            "Applied to all four samplers. Omit to keep the graph's stock seeds, "
            "which is the reproducible default."
        ),
    )
    target_face_count: int = Field(
        DEFAULT_FACE_COUNT,
        ge=MIN_FACE_COUNT,
        le=MAX_FACE_COUNT,
        description=(
            "Decimation target. With vertex colours this is a visual-quality knob, "
            "not just a file-size one: every face removed is colour detail lost."
        ),
    )
    bbox: Annotated[list[float], Field(min_length=4, max_length=4)] | None = Field(
        None,
        description=(
            "Where the athlete is, as [x0, y0, x1, y1] normalised to 0-1 against the "
            "displayed image. Omit to fall back to background removal over the whole "
            "frame, which cannot tell one person from another and merges anyone else "
            "in shot into the same subject."
        ),
    )

    @model_validator(mode="after")
    def _check_bbox(self):
        if self.bbox is None:
            return self
        x0, y0, x1, y1 = self.bbox
        if not all(0.0 <= v <= 1.0 for v in self.bbox):
            raise ValueError("bbox values must be normalised to 0-1")
        if x1 <= x0 or y1 <= y0:
            raise ValueError("bbox must have positive width and height (x0 < x1, y0 < y1)")
        return self


class JobCreateRequest(BaseModel):
    object_key: str
    params: JobParams = JobParams()


class JobCreateResponse(BaseModel):
    job_id: uuid.UUID


class StageTiming(BaseModel):
    stage: str
    seconds: float


class JobStatusResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    # The effective params, defaults filled in -- so a caller can confirm what
    # actually ran rather than what they meant to ask for. Typed as a dict
    # rather than JobParams so a row written under an older schema still reads.
    params: dict
    stage: str | None
    error: str | None
    stage_timings: list[StageTiming]
    total_seconds: float | None
    gpu_peak_mb: int | None
    coarse_glb_url: str | None
    final_glb_url: str | None
    final_glb_compressed_url: str | None
    # The URLs are re-signed on every read, so their string value changes even
    # when the underlying object has not. These keys are stable, and are what a
    # client should compare to decide whether the model actually changed.
    coarse_glb_key: str | None
    final_glb_key: str | None
    final_glb_compressed_key: str | None
    created_at: datetime
    updated_at: datetime


class ApiKeyCreateRequest(BaseModel):
    name: str


class ApiKeyCreateResponse(BaseModel):
    id: uuid.UUID
    name: str
    key: str  # plaintext -- shown exactly once, never stored or returned again


class ApiKeyInfo(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
