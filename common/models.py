import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_object_key: Mapped[str] = mapped_column(String(512))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage_timings: Mapped[list] = mapped_column(JSONB, default=list)

    coarse_glb_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    final_glb_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    final_glb_compressed_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Wall time of the pipeline run itself, excluding queue wait (which
    # updated_at - created_at would include). PLAN.md sec.4: per-stage GPU time
    # "matters a lot", and nothing recorded it.
    total_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Peak of the torch allocator, in MiB -- not nvidia-smi's number (see
    # worker/app/embedded_pipeline.py::_gpu_peak_mb). None on the http backend,
    # which runs in a different process from the GPU.
    gpu_peak_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    # Statuses: pending -> running -> succeeded | failed
    STATUSES = ("pending", "running", "succeeded", "failed")


class ApiKey(Base):
    __tablename__ = "api_keys"

    # "service" can run jobs; "admin" can also mint and revoke keys. Defaults to
    # the lesser of the two: a key handed to an integration should not be able
    # to issue itself more keys.
    SCOPES = ("service", "admin")

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255))
    scope: Mapped[str] = mapped_column(String(16), default="service", server_default="service")
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(128))
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
