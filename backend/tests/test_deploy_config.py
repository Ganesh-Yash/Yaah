"""The deployment surface is code too: entrypoint, Dockerfile and blueprint.

Nothing here starts a container — there is no Docker daemon in this environment
(see docs/VERIFICATION.md). What these tests can do is catch the mistakes that are
invisible locally and fatal on a PaaS: a hardcoded port, a migration that never
runs, a health check pointed at the wrong endpoint, a secret committed to the
blueprint.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from ledgerline.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
START_SH = REPO_ROOT / "backend" / "scripts" / "start.sh"
RENDER_YAML = REPO_ROOT / "render.yaml"


def test_entrypoint_migrates_the_database_before_serving():
    """A fresh deploy must never serve traffic against a schema that is not there."""
    script = START_SH.read_text()
    assert "set -eu" in script
    assert "python -m scripts.migrate" in script
    assert script.index("python -m scripts.migrate") < script.index("exec uvicorn")
    # Failing migration must abort the boot rather than fall through to uvicorn.
    assert re.search(r"^\s*(set -eu)$", script, re.MULTILINE)


def test_entrypoint_honours_the_port_the_platform_assigns():
    """Render injects PORT; a hardcoded 8000 is a 502 with a healthy-looking log."""
    script = START_SH.read_text()
    assert "${PORT:-8000}" in script, "start.sh must read $PORT, with 8000 as the fallback"

    dockerfile = DOCKERFILE.read_text()
    assert "scripts/start.sh" in dockerfile, "the image must start via the entrypoint"
    assert '"--port", "8000"' not in dockerfile, "the image must not hardcode the port"
    # The container health check has to follow the port too, or it reports the
    # service unhealthy purely because the platform relocated it.
    assert "os.environ.get('PORT'" in dockerfile


def test_image_ships_the_migration_sql():
    """pg.py resolves /app/prisma/migrations; the image has to contain it."""
    dockerfile = DOCKERFILE.read_text()
    assert "COPY prisma/ ./prisma/" in dockerfile


@pytest.fixture(scope="module")
def blueprint() -> dict:
    return yaml.safe_load(RENDER_YAML.read_text())


def test_blueprint_declares_one_docker_service(blueprint):
    services = blueprint["services"]
    assert len(services) == 1, "one service: the API serves the built SPA itself"

    service = services[0]
    assert service["type"] == "web"
    assert service["runtime"] == "docker"
    assert service["dockerfilePath"].lstrip("./") == "Dockerfile"
    # The plan must be *stated*. Render defaults to a paid instance type when the
    # key is absent, so omitting it silently turns a free deploy into a bill.
    assert service["plan"] in {"free", "starter"}
    # Same trap, different consequence: this key decides which commit is built,
    # and the dashboard's branch picker does not override it. A blueprint that
    # deploys a branch without the code it was configured for is worse than no
    # blueprint — every setting looks right and the deployed image is older.
    assert service["branch"], "the deployed branch must be stated explicitly"
    assert service["region"] == "singapore"


def test_blueprint_health_check_proves_the_database_works(blueprint):
    """Readiness, not liveness: /api/health is 200 even with no schema."""
    assert blueprint["services"][0]["healthCheckPath"] == "/api/ready"


def test_blueprint_keeps_secrets_out_of_the_repository(blueprint):
    env = {item["key"]: item for item in blueprint["services"][0]["envVars"]}

    assert env["LEDGERLINE_ENV"]["value"] == "prod"
    assert env["LEDGERLINE_JWT_SECRET"]["generateValue"] is True

    # Connection strings are set in the dashboard; `sync: false` keeps the
    # blueprint from writing them back to the repository.
    for key in ("LEDGERLINE_DATABASE_URL", "LEDGERLINE_REDIS_URL"):
        assert env[key] == {"key": key, "sync": False}

    for key, item in env.items():
        assert "value" not in item or key == "LEDGERLINE_ENV", f"{key} carries a literal value"


def test_render_hostname_is_used_as_the_default_allowlist(monkeypatch):
    """Render tells us the public host; a missing allowlist is a 400 on every request."""
    monkeypatch.delenv("LEDGERLINE_TRUSTED_HOSTS", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "BalanceVault.onrender.com")

    assert Settings.from_env().trusted_hosts == ("balancevault.onrender.com",)


def test_explicit_host_allowlist_beats_the_platform_default(monkeypatch):
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "balancevault.onrender.com")
    monkeypatch.setenv("LEDGERLINE_TRUSTED_HOSTS", "ledger.example.com,www.ledger.example.com")

    settings = Settings.from_env()
    assert settings.trusted_hosts == ("ledger.example.com", "www.ledger.example.com")


def test_no_platform_hostname_means_no_allowlist(monkeypatch):
    """Off Render the middleware stays off rather than inventing a host to trust."""
    monkeypatch.delenv("LEDGERLINE_TRUSTED_HOSTS", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_HOSTNAME", raising=False)

    assert Settings.from_env().trusted_hosts == ()

def test_blueprint_does_not_enable_demo_seeding(blueprint):
    """seed_demo() refuses in prod, so enabling it here crash-loops the service.

    The demo password is published in this repository; the guard is the only thing
    standing between a fork and a public admin account, so it stays a guard — the
    blueprint must not suggest a variable that turns the deploy into a boot loop.
    """
    env = {item["key"] for item in blueprint["services"][0]["envVars"]}
    assert "LEDGERLINE_DEMO_SEED" not in env

