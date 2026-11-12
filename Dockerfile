# syntax=docker/dockerfile:1

# ---------- stage 1: build the React SPA ------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------- stage 2: runtime -------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    LEDGERLINE_DATABASE_PATH=/data/ledgerline.db \
    LEDGERLINE_STATIC_DIR=/app/frontend/dist \
    LEDGERLINE_ENV=prod

WORKDIR /app

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ ./backend/
# The migration SQL ships with the image so the release step (scripts.migrate)
# can bootstrap an empty database. pg.py resolves it at /app/prisma/migrations.
COPY prisma/ ./prisma/
COPY --from=frontend /build/frontend/dist ./frontend/dist

# Unprivileged runtime user; /data is the only writable path it needs.
RUN useradd --create-home --uid 10001 ledger \
 && mkdir -p /data \
 && chown -R ledger:ledger /data /app
USER ledger

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

WORKDIR /app/backend
# One worker on purpose: SQLite has a single writer. See docs/DEPLOYMENT.md.
CMD ["uvicorn", "ledgerline.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
