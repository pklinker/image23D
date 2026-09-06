import uuid
from datetime import datetime

from pydantic import BaseModel


class UploadRequest(BaseModel):
    filename: str
    content_type: str = "image/png"


class UploadResponse(BaseModel):
    object_key: str
    upload_url: str


class JobCreateRequest(BaseModel):
    object_key: str
    params: dict = {}


class JobCreateResponse(BaseModel):
    job_id: uuid.UUID


class StageTiming(BaseModel):
    stage: str
    seconds: float


class JobStatusResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    stage: str | None
    error: str | None
    stage_timings: list[StageTiming]
    total_seconds: float | None
    gpu_peak_mb: int | None
    coarse_glb_url: str | None
    final_glb_url: str | None
    final_glb_compressed_url: str | None
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
