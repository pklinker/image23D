import time

from fastapi import Depends, HTTPException, Request

from common.models import ApiKey
from common.settings import settings

from .auth import require_api_key


def rate_limit(bucket: str, limit: int, window_seconds: int = 60):
    """Fixed-window limiter: `limit` calls per `window_seconds` per API key
    per bucket. Good enough at this project's scale -- no need for a token
    bucket's smoother burst handling yet."""

    async def _dependency(request: Request, api_key: ApiKey = Depends(require_api_key)) -> ApiKey:
        redis = request.app.state.redis
        window = int(time.time()) // window_seconds
        redis_key = f"ratelimit:{bucket}:{api_key.id}:{window}"

        count = await redis.incr(redis_key)
        if count == 1:
            await redis.expire(redis_key, window_seconds)
        if count > limit:
            raise HTTPException(429, f"rate limit exceeded: {limit}/{window_seconds}s for {bucket}")
        return api_key

    return _dependency


# PLAN.md's own load assumption: "low volume, multi-minute turnaround
# acceptable" -- one GPU, concurrency 1. These are generous relative to that,
# meant to catch a runaway client/bug, not to shape legitimate traffic.
require_job_creation_rate_limit = rate_limit("job_create", limit=settings.rate_limit_job_creation_per_minute)
require_upload_rate_limit = rate_limit("upload", limit=settings.rate_limit_upload_per_minute)
