from arq.connections import RedisSettings

from common.settings import settings
from worker.app.tasks import run_pipeline_job


class WorkerSettings:
    functions = [run_pipeline_job]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    # GPU jobs are long and serial -- PLAN.md sec.4: concurrency = 1 per GPU.
    max_jobs = 1
    job_timeout = 900
