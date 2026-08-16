# syntax=docker/dockerfile:1

FROM node:22-slim AS webapp-builder
WORKDIR /webapp
COPY webapp/package.json webapp/package-lock.json ./
RUN npm ci
COPY webapp/ ./
RUN npm run build

FROM python:3.12-slim

# opencv-python-headless and numpy dynamically link these at runtime; the
# slim base doesn't ship them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps in their own layer so code-only changes don't bust the cache.
COPY requirements.txt requirements-server.txt ./
RUN pip install --no-cache-dir -r requirements-server.txt

COPY backend_app.py pig_tracking_pipeline.py pig_stress_monitor.py ./
COPY pigs_top_down.mp4 sensors_sample.json ./
COPY --from=webapp-builder /webapp/dist ./webapp_dist

RUN useradd --system --create-home --uid 10001 pigwatch \
    && chown -R pigwatch:pigwatch /app
USER pigwatch

# Demo assets are baked in so the container runs standalone out of the box.
# Point these at real data instead via `docker run -e PIGWATCH_VIDEO=... -e
# PIGWATCH_SENSORS=...`. There is no database in this app to configure.
# CRUSOE_API_KEY is a secret: it has NO default here and must be supplied at
# run time (-e, --env-file, or an orchestrator secret) -- never baked into
# the image or this file.
ENV PIGWATCH_VIDEO=/app/pigs_top_down.mp4 \
    PIGWATCH_SENSORS=/app/sensors_sample.json \
    PIGWATCH_ENCLOSURE_ID=01 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health', timeout=2)" || exit 1

CMD ["uvicorn", "backend_app:app", "--host", "0.0.0.0", "--port", "8000"]
