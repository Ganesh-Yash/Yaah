# BalanceVault API. Python runs the application; Node runs migrations only, in a
# separate container (deploy/docker-compose.yml), so this image has no Node in it.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: this layer survives source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user; the database, not the filesystem, holds the state.
RUN useradd --create-home --uid 10001 balancevault && chown -R balancevault /app
USER balancevault

EXPOSE 8000

# Readiness is /api/ready (503 until the schema is present), not /api/health:
# a container that cannot reach its database should not receive traffic.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/ready').status==200 else 1)"

CMD ["uvicorn", "ledgerline.main:app", "--host", "0.0.0.0", "--port", "8000"]
