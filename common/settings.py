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

    comfy_base_url: str = "http://comfy-worker:8188"
    comfy_shared_input_dir: str = "/shared/input"
    comfy_shared_output_dir: str = "/shared/output"

    presigned_url_ttl_seconds: int = 3600


settings = Settings()
