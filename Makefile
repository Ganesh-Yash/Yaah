# Convenience targets. Everything here is also documented in the README.

PY ?= ../.venv/bin/python
BASE_URL ?= http://localhost:8000

.PHONY: help install seed dev test redteam build docker clean

help:
	@echo "install   create .venv and install backend + frontend dependencies"
	@echo "seed      create the database and demo users"
	@echo "dev       run the API on :8000 (serves the built SPA if frontend/dist exists)"
	@echo "test      run the backend test suite (153 tests)"
	@echo "redteam   attack a running instance ($(BASE_URL))"
	@echo "build     build the frontend into frontend/dist"
	@echo "docker    build and start the compose stack"
	@echo "clean     remove the local database and build artefacts"

install:
	python3 -m venv .venv
	.venv/bin/pip install -q --upgrade pip
	.venv/bin/pip install -q -r backend/requirements-dev.txt
	cd frontend && npm install --no-audit --no-fund

seed:
	cd backend && ../.venv/bin/python scripts/seed.py --demo

dev:
	cd backend && LEDGERLINE_STATIC_DIR=$$(pwd)/../frontend/dist \
		LEDGERLINE_DEMO_SEED=1 ../.venv/bin/uvicorn ledgerline.main:app --reload --port 8000

test:
	cd backend && ../.venv/bin/python -m pytest -q

redteam:
	cd backend && ../.venv/bin/python scripts/redteam.py --base-url $(BASE_URL)

build:
	cd frontend && npm run build

docker:
	@test -f .env || (echo "create .env from .env.example first" && exit 1)
	docker compose up --build -d

clean:
	rm -rf data frontend/dist backend/.pytest_cache backend/**/__pycache__ .pytest_cache
