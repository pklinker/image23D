FROM python:3.12-slim-bookworm

WORKDIR /app

COPY api/requirements.txt /app/api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY common /app/common
COPY api /app/api

ENV PYTHONPATH=/app

EXPOSE 8000

ENTRYPOINT ["uvicorn", "api.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
