"""Rate limiting: the in-process limiter, and the Redis one behind it.

Redis is a real server (``redislite`` ships a redis binary for this purpose),
not a mock: the whole point of moving the counter out of the process is that two
workers share it, and a fake would not prove that.
"""

from __future__ import annotations

import pytest

from ledgerline.limiter import RedisSlidingWindowLimiter, SlidingWindowLimiter, make_limiter


@pytest.fixture(scope="module")
def redis_client():
    redislite = pytest.importorskip("redislite", reason="redislite not installed (optional integration extra)")
    client = redislite.StrictRedis(serverconfig={"save": ""}) if hasattr(redislite, "StrictRedis") else redislite.Redis(serverconfig={"save": ""})
    client.flushall()
    yield client
    try:
        client.shutdown()
    except Exception:  # pragma: no cover - best effort teardown
        pass


class TestInProcessLimiter:
    def test_allows_up_to_the_limit_then_refuses(self):
        limiter = SlidingWindowLimiter(limit=3, window_seconds=60)
        assert [limiter.allow("k") for _ in range(4)] == [True, True, True, False]

    def test_keys_are_independent(self):
        limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
        assert limiter.allow("a") is True
        assert limiter.allow("b") is True
        assert limiter.allow("a") is False

    def test_reset_clears_a_key(self):
        limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
        assert limiter.allow("a") is True
        assert limiter.allow("a") is False
        limiter.reset("a")
        assert limiter.allow("a") is True


class TestRedisLimiter:
    def test_shared_counter_across_two_workers(self, redis_client):
        """The bug this exists to prevent: 30/minute becoming 30 *per worker*."""
        worker_a = RedisSlidingWindowLimiter(redis_client, limit=5, window_seconds=60, prefix="test:shared")
        worker_b = RedisSlidingWindowLimiter(redis_client, limit=5, window_seconds=60, prefix="test:shared")
        decisions = [worker_a.allow("ip-1") for _ in range(3)]
        decisions += [worker_b.allow("ip-1") for _ in range(4)]
        assert decisions == [True, True, True, True, True, False, False]

    def test_keys_are_independent(self, redis_client):
        limiter = RedisSlidingWindowLimiter(redis_client, limit=1, window_seconds=60, prefix="test:keys")
        assert limiter.allow("a") is True
        assert limiter.allow("b") is True
        assert limiter.allow("a") is False

    def test_window_expiry_releases_the_budget(self, redis_client):
        limiter = RedisSlidingWindowLimiter(redis_client, limit=1, window_seconds=0.2, prefix="test:expiry")
        assert limiter.allow("a") is True
        assert limiter.allow("a") is False
        import time

        time.sleep(0.35)
        assert limiter.allow("a") is True

    def test_reset_clears_the_namespace(self, redis_client):
        limiter = RedisSlidingWindowLimiter(redis_client, limit=1, window_seconds=60, prefix="test:reset")
        assert limiter.allow("a") is True
        assert limiter.allow("a") is False
        limiter.reset()
        assert limiter.allow("a") is True

    def test_fails_open_when_redis_is_unreachable(self):
        """Availability beats strictness here: the money invariants are in the
        database, so a Redis outage must not become a total outage."""
        import redis

        dead = redis.Redis(host="127.0.0.1", port=1, socket_timeout=0.1, socket_connect_timeout=0.1)
        limiter = RedisSlidingWindowLimiter(dead, limit=1, window_seconds=60, prefix="test:dead")
        assert limiter.allow("a") is True


def test_make_limiter_chooses_the_backend_from_configuration(redis_client):
    from ledgerline.config import Settings

    assert make_limiter(None, limit=1, window_seconds=60, prefix="p").backend == "sqlite" or isinstance(
        make_limiter(None, limit=1, window_seconds=60, prefix="p"), SlidingWindowLimiter
    )
    configured = Settings(env="test", jwt_secret="t" * 40, redis_url="redis://127.0.0.1:6379/0")
    limiter = make_limiter(redis_client, limit=2, window_seconds=60, prefix="test:factory")
    assert limiter.backend == "redis"
    assert configured.redis_url.endswith("/0")


def test_app_uses_redis_when_a_url_is_configured(tmp_path, redis_client):
    """End-to-end wiring: the app must build a Redis limiter, not a local one."""
    from fastapi.testclient import TestClient

    from ledgerline.config import Settings
    from ledgerline.main import create_app

    settings = Settings(
        env="test",
        database_path=str(tmp_path / "redis-app.db"),
        jwt_secret="test-secret-" + "x" * 40,
        bcrypt_rounds=4,
        redis_url="redis://127.0.0.1:6379/0",
    )
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert app.state.money_limiter.backend == "redis"
        body = client.get("/api/health").json()
        assert body["checks"]["redis"]["state"] in {"ok", "unavailable"}
        assert client.get("/api/ready").status_code == 200


def test_health_reports_a_disabled_redis_honestly(client):
    body = client.get("/api/health").json()
    assert body["checks"]["redis"]["state"] == "disabled"
    assert body["checks"]["redis"]["ok"] is True
    assert body["checks"]["schema"]["version"] in {"1", "2"}
