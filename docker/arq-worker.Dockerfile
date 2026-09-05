# ARQ orchestration worker: talks HTTP/WS to the comfy-worker GPU container
# (docker/worker.Dockerfile) -- no torch/CUDA here, this is CPU-only glue code.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY worker/requirements.txt /app/worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt

COPY common /app/common
COPY worker /app/worker

ENV PYTHONPATH=/app

ENTRYPOINT ["arq", "worker.app.worker_settings.WorkerSettings"]
