"""Transport-level behaviour: error shapes, headers, size limits, CORS."""

from __future__ import annotations


def test_health_and_meta(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["database"] == "ok"

    meta = client.get("/api/meta")
    assert meta.status_code == 200
    assert meta.json()["name"] == "LedgerLine"
    assert "Idempotency-Key" in meta.json()["idempotency_header"]


def test_unknown_route_uses_the_error_envelope(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["request_id"].startswith("req_")
    assert "Traceback" not in response.text


def test_method_not_allowed(client):
    response = client.delete("/api/health")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_security_headers_on_every_response(client):
    for path in ("/api/health", "/api/meta", "/api/nope"):
        response = client.get(path)
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors" in response.headers["Content-Security-Policy"]
        assert response.headers["X-Request-ID"]


def test_api_responses_are_not_cacheable(client):
    assert client.get("/api/health").headers["Cache-Control"] == "no-store"
    assert client.get("/api/meta").headers["Cache-Control"] == "no-store"


def test_large_bodies_are_rejected(client):
    payload = {"amount": "1.00", "description": "x" * 100_000}
    response = client.post(
        "/api/auth/login", json={"email": "a@example.com", "password": "x" * 100_000, **payload}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_cors_allows_only_configured_origins(client):
    allowed = client.options(
        "/api/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert allowed.status_code in (200, 204)
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"

    denied = client.options(
        "/api/health",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert denied.headers.get("access-control-allow-origin") is None


def test_internal_errors_do_not_leak_details(client, app, admin, monkeypatch):
    from ledgerline.services import accounts_service

    def explode(*args, **kwargs):
        raise RuntimeError("SQLITE_SECRET_DETAIL: /var/lib/ledgerline/ledger.db")

    monkeypatch.setattr(accounts_service, "list_accounts", explode)
    response = client.get("/api/accounts", headers=admin)
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "SQLITE_SECRET_DETAIL" not in response.text
    assert "Traceback" not in response.text
    assert body["error"]["request_id"].startswith("req_")


def test_validation_errors_do_not_echo_the_password(client):
    secret = "ThisIsASecretPassword123"
    response = client.post(
        "/api/auth/login", json={"email": "x", "password": secret, "extra": "nope"}
    )
    assert response.status_code == 422
    assert secret not in response.text


def test_openapi_is_available_for_reviewers(client):
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/api/transfers" in paths
    assert "/api/auditor/integrity" in paths
