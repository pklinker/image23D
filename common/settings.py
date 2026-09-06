from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://image23d:image23d@postgres:5432/image23d"
    redis_url: str = "redis://redis:6379/0"

    s3_endpoint_url: str = "http://minio:9000"
    s3_public_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "image23d"
    s3_secret_key: str = "image23d-dev-secret"
    s3_bucket: str = "image23d"
    s3_region: str = "us-east-1"

    # comfy_base_url only matters for pipeline_backend="http" (see below) --
    # the default "embedded" backend never makes an HTTP call.
    comfy_base_url: str = "http://comfy-worker:8188"
    # Phase 4: these are ComfyUI's own default input/output dirs (relative to
    # vendor/ComfyUI's location, see folder_paths.py), not a separate shared
    # volume mount point like Phase 2-3's CPU-only worker used.
    comfy_shared_input_dir: str = "/app/ComfyUI/input"
    comfy_shared_output_dir: str = "/app/ComfyUI/output"

    presigned_url_ttl_seconds: int = 3600

    # Installed globally in the worker image (docker/gpu-worker.Dockerfile).
    # Overridable so a dev box can point at a local install without a rebuild.
    gltf_transform_bin: str = "gltf-transform"
    gltf_transform_timeout_seconds: int = 300

    # Phase 3: viewer dev server origin(s) allowed to call the API directly.
    viewer_origins: list[str] = ["http://localhost:5173"]

    # Phase 4: hardening.
    rate_limit_job_creation_per_minute: int = 10
    rate_limit_upload_per_minute: int = 20
    retention_days: int = 30

    # Bug-fix item 1: the pipeline enforces its own deadline rather than
    # relying on ARQ's job_timeout. ARQ implements a timeout by cancelling the
    # job task, and the GPU work runs in an asyncio.to_thread that cancellation
    # cannot actually stop -- so the only thing that ends the run is ComfyUI's
    # cooperative interrupt flag, which the pipeline has to raise itself.
    # ARQ's job_timeout is left as a backstop well above these (see
    # worker/app/worker_settings.py).
    pipeline_timeout_seconds: int = 900
    # How long to wait for ComfyUI's thread to notice the interrupt and exit
    # before giving up on it. Nodes only check the flag between steps, so this
    # has to cover the slowest single node (~15s on the reference workflow).
    pipeline_interrupt_grace_seconds: int = 120

    # Phase 4: "embedded" (default) imports ComfyUI in-process, see
    # worker/app/embedded_pipeline.py. "http" is the original Phase 1/2 path,
    # kept for debugging -- it requires something separately running and
    # listening at comfy_base_url, which the default stack no longer does.
    pipeline_backend: str = "embedded"


settings = Settings()
