# Image for the TCP ingestion gateway (deploy/aws, beside the API's image,
# Dockerfile.api).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GATEWAY_LOG_DIR=/var/log/gps-tracking

WORKDIR /app

# Only asyncpg is needed; fastapi/uvicorn belong to the API deployment.
COPY requirements-gateway.txt .
RUN pip install --no-cache-dir -r requirements-gateway.txt

COPY weekly_logs.py ./
COPY gps_gateway/ ./gps_gateway/
COPY sql/ ./sql/

# Run as a non-root user: this process is exposed directly to the internet.
RUN useradd --create-home --uid 10001 gateway \
    && mkdir -p /var/log/gps-tracking && chown gateway /var/log/gps-tracking
USER gateway

EXPOSE 5023

CMD ["python", "-m", "gps_gateway"]
