# Works on any host that accepts a container: Koyeb, Fly.io, Google Cloud Run,
# Oracle Cloud, a VPS, or your own machine.
#
#   docker build -t telegram-youtube-gate .
#   docker run --rm --env-file .env -v "$(pwd)/data:/app/data" telegram-youtube-gate

FROM python:3.11-slim

# Never write .pyc files; always flush logs so `docker logs` is live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so this layer is cached between code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user. `data` is chowned so SQLite can write there.
RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app
USER botuser

# Only meaningful when HEALTH_CHECK_PORT/PORT is set (Phase 2, or a host that
# requires an open port). Harmless otherwise.
EXPOSE 8080

CMD ["python", "run.py"]
