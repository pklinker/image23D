from arq import cron
from arq.connections import RedisSettings

from common.settings import settings
from worker.app.embedded_pipeline import bootstrap_comfy
from worker.app.tasks import purge_old_jobs, run_pipeline_job


async def on_startup(ctx) -> None:
    if settings.pipeline_backend == "embedded":
        # Runs once before the first job, on the loop that will service all
        # jobs -- see embedded_pipeline.py's _StubServer for why the loop
        # identity at bootstrap time matters.
        await bootstrap_comfy()


class WorkerSettings:
    functions = [run_pipeline_job]
    on_startup = on_startup
    # PLAN.md sec.4 retention policy: daily sweep of jobs past retention_days.
    cron_jobs = [cron(purge_old_jobs, hour=3, minute=0)]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    # GPU jobs are long and serial -- PLAN.md sec.4: concurrency = 1 per GPU.
    max_jobs = 1
    job_timeout = 900
