#!/bin/sh
# Container entrypoint, shared by every platform (Fly, Render, plain docker run).
#
# Two jobs, in order:
#
#   1. bring the database to the schema this image expects. Idempotent, so it is
#      safe on every boot — and on Render it is the *only* place the migration
#      can run, because a pre-deploy command is not available on every plan.
#   2. exec the server on $PORT. Render assigns that port dynamically; a
#      hardcoded 8000 produces a 502 from the load balancer while the process
#      itself looks perfectly healthy in the logs. Fly's internal_port still
#      works, because 8000 is the fallback.
#
# Set LEDGERLINE_SKIP_STARTUP_MIGRATION=1 when the platform already runs
# `python -m scripts.migrate` as a separate pre-traffic release step and you do
# not want it repeated (Fly's release_command is exactly that).
set -eu

if [ "${LEDGERLINE_SKIP_STARTUP_MIGRATION:-0}" != "1" ]; then
    python -m scripts.migrate
fi

exec uvicorn ledgerline.main:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1
