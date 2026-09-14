# Image for the TCP ingestion gateway (Fly.io).
# The REST API deploys separately to Vercel and does not use this file.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Only asyncpg is needed; fastapi/uvicorn belong to the API deployment.
COPY requirements-gateway.txt .
RUN pip install --no-cache-dir -r requirements-gateway.txt

COPY gps_gateway/ ./gps_gateway/
COPY sql/ ./sql/

# Run as a non-root user: this process is exposed directly to the internet.
RUN useradd --create-home --uid 10001 gateway
USER gateway

EXPOSE 5023

CMD ["python", "-m", "gps_gateway"]
