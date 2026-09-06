from arq import cron, func
from arq.connections import RedisSettings

from common.settings import settings
from worker.app.embedded_pipeline import bootstrap_comfy
from worker.app.pipeline import purge_orphaned_scratch_files
from worker.app.tasks import fail_orphaned_jobs, purge_old_jobs, run_pipeline_job


async def on_startup(ctx) -> None:
    # Before anything else: a job still marked `running` at startup was owned by
    # a worker that died mid-job, and nothing else will ever finish it.
    await fail_orphaned_jobs()
    # Scratch files leaked by earlier worker lifetimes. Safe here: max_jobs=1
    # means nothing is in flight at startup.
    purge_orphaned_scratch_files()

    if settings.pipeline_backend == "embedded":
        # Runs once before the first job, on the loop that will service all
        # jobs -- see embedded_pipeline.py's _StubServer for why the loop
        # identity at bootstrap time matters.
        await bootstrap_comfy()


class WorkerSettings:
    # max_tries=1: never retry. A GPU job holds the card for ~70s exclusively,
    # and the pipeline is deterministic on a fixed input, so a retry of a
    # genuinely-broken job just burns the GPU twice for the same failure.
    # Worse, ARQ's default behaviour on a cancelled job is to re-enqueue it --
    # and because the GPU work runs in an asyncio.to_thread that cancellation
    # cannot stop, that retry would start a second run while the first still
    # holds ~12.9GB of the 16GB card.
    functions = [func(run_pipeline_job, max_tries=1)]
    on_startup = on_startup
    # PLAN.md sec.4 retention policy: daily sweep of jobs past retention_days.
    cron_jobs = [cron(purge_old_jobs, hour=3, minute=0)]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    # GPU jobs are long and serial -- PLAN.md sec.4: concurrency = 1 per GPU.
    max_jobs = 1
    # Backstop only. The real deadline is enforced inside the pipeline
    # (settings.pipeline_timeout_seconds) because ARQ implements a timeout by
    # cancelling the job task, which cannot stop the GPU thread. This sits above
    # the pipeline's deadline plus the grace period it allows for the ComfyUI
    # thread to notice the interrupt, so it only fires if that machinery itself
    # is stuck.
    job_timeout = settings.pipeline_timeout_seconds + settings.pipeline_interrupt_grace_seconds + 60
