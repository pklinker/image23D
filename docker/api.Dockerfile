FROM python:3.12-slim-bookworm

WORKDIR /app

COPY api/requirements.txt /app/api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY common /app/common
COPY api /app/api
COPY alembic.ini /app/alembic.ini
COPY alembic /app/alembic
# Bootstrapping the first API key has to happen somewhere that can reach the
# database, and there is no unauthenticated route that can mint one -- so the
# script ships in the image and is run with `docker compose exec api`.
COPY scripts /app/scripts

ENV PYTHONPATH=/app

EXPOSE 8000

ENTRYPOINT ["sh", "-c", "alembic upgrade head && uvicorn api.app.main:app --host 0.0.0.0 --port 8000"]
