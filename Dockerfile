FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# config/ is intentionally NOT copied here - docker-compose.yml mounts
# deploy/compose/config.yaml over it, so the demo stack's config (mock
# provider URLs, compose-network redis) is what actually gets used.
COPY config ./config

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
