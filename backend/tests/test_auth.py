"""Authentication: registration, login, tokens, rotation and lockout.

The negative cases here map 1:1 to the attacks a reviewer will try on the
deployed system.
"""

from __future__ import annotations

import time

import jwt
import pytest

from conftest import PASSWORD


class TestRegistration:
    def test_register_and_login(self, api, client):
        response = client.post(
            "/api/auth/register",
            json={"email": "New.User@Example.com", "password": PASSWORD, "full_name": "New User"},
        )
        assert response.status_code == 201, response.text
        user = response.json()["user"]
        assert user["email"] == "new.user@example.com"  # normalised
        assert user["role"] == "CUSTOMER"
        assert "password" not in str(user).lower() or "password_hash" not in user

        tokens = api.login("new.user@example.com")
        assert tokens["token_type"] == "bearer"
        assert tokens["expires_in"] == 900

    def test_duplicate_email_is_a_conflict(self, api, client):
        payload = {"email": "dupe@example.com", "password": PASSWORD, "full_name": "Dupe User"}
        assert client.post("/api/auth/register", json=payload).status_code == 201
        second = client.post("/api/auth/register", json=payload)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"

    def test_role_cannot_be_escalated_through_the_payload(self, client):
        """Mass assignment: adding "role" must be rejected, not ignored."""
        response = client.post(
            "/api/auth/register",
            json={
                "email": "sneaky@example.com",
                "password": PASSWORD,
                "full_name": "Sneaky User",
                "role": "ADMIN",
            },
        )
        assert response.status_code == 422
        assert "role" in response.text

    @pytest.mark.parametrize(
        "password",
        [
            "short",
            "password",  # common
            "password1234",  # common
            "aaaaaaaaaaaaaaaa",  # repetitive
            "x" * 73,  # bcrypt truncation boundary
        ],
    )
    def test_weak_passwords_rejected(self, client, password):
        response = client.post(
            "/api/auth/register",
            json={"email": "weak@example.com", "password": password, "full_name": "Weak User"},
        )
        assert response.status_code == 422, response.text

    @pytest.mark.parametrize("email", ["not-an-email", "a@b", "a@@b.com", "@example.com", "x" * 250 + "@b.com"])
    def test_invalid_emails_rejected(self, client, email):
        response = client.post(
            "/api/auth/register",
            json={"email": email, "password": PASSWORD, "full_name": "Bad Email"},
        )
        assert response.status_code == 422


class TestLogin:
    def test_unknown_email_and_wrong_password_are_indistinguishable(self, api, client):
        api.create_user("real@example.com")
        wrong = client.post("/api/auth/login", json={"email": "real@example.com", "password": "WrongPassword123"})
        unknown = client.post("/api/auth/login", json={"email": "ghost@example.com", "password": "WrongPassword123"})
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"] == "INVALID_CREDENTIALS"
        assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]

    def test_account_locks_after_repeated_failures(self, api, client):
        api.create_user("victim@example.com")
        for _ in range(5):
            response = client.post(
                "/api/auth/login", json={"email": "victim@example.com", "password": "WrongPassword123"}
            )
            assert response.status_code == 401
        locked = client.post(
            "/api/auth/login", json={"email": "victim@example.com", "password": PASSWORD}
        )
        assert locked.status_code == 429
        assert locked.json()["error"]["code"] == "RATE_LIMITED"

    def test_successful_login_resets_the_failure_counter(self, api, client):
        api.create_user("reset@example.com")
        for _ in range(3):
            client.post("/api/auth/login", json={"email": "reset@example.com", "password": "WrongPassword123"})
        assert client.post(
            "/api/auth/login", json={"email": "reset@example.com", "password": PASSWORD}
        ).status_code == 200
        with api.db.read() as conn:
            row = conn.execute("SELECT failed_login_count FROM users WHERE email = 'reset@example.com'").fetchone()
        assert row["failed_login_count"] == 0

    def test_disabled_user_cannot_log_in(self, api, client):
        api.create_user("disabled@example.com", status="DISABLED")
        response = client.post(
            "/api/auth/login", json={"email": "disabled@example.com", "password": PASSWORD}
        )
        assert response.status_code == 401


class TestTokens:
    def test_missing_and_malformed_headers(self, client):
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401
        assert client.get("/api/auth/me", headers={"Authorization": "Bearer"}).status_code == 401
        assert client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401

    def test_token_signed_with_another_secret_is_rejected(self, api, client, settings):
        api.create_user("forger@example.com")
        tokens = api.login("forger@example.com")
        now = int(time.time())
        forged = jwt.encode(
            {
                "sub": "someone",
                "typ": "access",
                "iat": now,
                "nbf": now,
                "exp": now + 900,
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,
            },
            "attacker-secret-attacker-secret-123",
            algorithm="HS256",
        )
        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
        assert response.status_code == 401
        # sanity: the legitimate token still works
        assert client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        ).status_code == 200

    def test_alg_none_token_is_rejected(self, api, client, settings):
        api.create_user("algnone@example.com")
        user_id = api.login("algnone@example.com")["user"]["id"]
        now = int(time.time())
        unsigned = jwt.encode(
            {
                "sub": user_id,
                "typ": "access",
                "iat": now,
                "nbf": now,
                "exp": now + 900,
                "iss": settings.jwt_issuer,
                "aud": settings.jwt_audience,
            },
            key="",
            algorithm="none",
        )
        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {unsigned}"})
        assert response.status_code == 401

    def test_expired_token_is_rejected(self, api, client, settings):
        api.create_user("expired@example.com")
        tokens = api.login("expired@example.com")
        claims = jwt.decode(
            tokens["access_token"], settings.jwt_secret, algorithms=["HS256"],
            audience=settings.jwt_audience, issuer=settings.jwt_issuer,
        )
        claims["exp"] = int(time.time()) - 60
        claims["iat"] = int(time.time()) - 1200
        claims["nbf"] = int(time.time()) - 1200
        expired = jwt.encode(claims, settings.jwt_secret, algorithm="HS256")
        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
        assert response.status_code == 401
        assert "expired" in response.json()["error"]["message"].lower()

    def test_wrong_audience_and_issuer_are_rejected(self, api, client, settings):
        api.create_user("aud@example.com")
        user_id = api.login("aud@example.com")["user"]["id"]
        now = int(time.time())
        for iss, aud in (("other-issuer", settings.jwt_audience), (settings.jwt_issuer, "other-api")):
            token = jwt.encode(
                {
                    "sub": user_id, "typ": "access", "iat": now, "nbf": now,
                    "exp": now + 900, "iss": iss, "aud": aud,
                    "pwd": "0000000000000000",
                },
                settings.jwt_secret,
                algorithm="HS256",
            )
            assert client.get(
                "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
            ).status_code == 401

    def test_refresh_token_metadata_is_not_accepted_as_access_token(self, api, client):
        api.create_user("typ@example.com")
        tokens = api.login("typ@example.com")
        response = client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
        )
        assert response.status_code == 401

    def test_deleted_user_token_stops_working(self, api, client):
        user_id = api.create_user("gone@example.com")
        tokens = api.login("gone@example.com")
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert client.get("/api/auth/me", headers=headers).status_code == 200
        with api.db.write() as conn:
            conn.execute("DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,))
            conn.execute("UPDATE users SET status = 'DISABLED' WHERE id = ?", (user_id,))
        assert client.get("/api/auth/me", headers=headers).status_code == 403


class TestRefreshRotation:
    def test_rotation_issues_a_new_pair_and_invalidates_the_old_token(self, api, client):
        api.create_user("rotate@example.com")
        first = api.login("rotate@example.com")
        response = client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert response.status_code == 200, response.text
        second = response.json()
        assert second["refresh_token"] != first["refresh_token"]

        reused = client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert reused.status_code == 401
        assert reused.json()["error"]["code"] == "UNAUTHENTICATED"

    def test_reuse_revokes_the_whole_family(self, api, client, auditor):
        api.create_user("family@example.com")
        first = api.login("family@example.com")
        second = client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]}).json()

        # Attacker replays the stolen, already-rotated token ...
        stolen = client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert stolen.status_code == 401
        # ... and the legitimate holder's newest token is dead too.
        killed = client.post("/api/auth/refresh", json={"refresh_token": second["refresh_token"]})
        assert killed.status_code == 401

        log = client.get("/api/audit?action=REFRESH_TOKEN_REUSE", headers=auditor).json()
        assert log["entries"], "token reuse must be recorded in the audit trail"
        assert log["entries"][0]["outcome"] == "DENY"

    def test_logout_revokes_refresh_token(self, api, client):
        api.create_user("logout@example.com")
        tokens = api.login("logout@example.com")
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert client.post(
            "/api/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
        ).status_code == 204
        assert client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code == 401

    def test_password_change_kills_old_sessions(self, api, client):
        api.create_user("change@example.com")
        tokens = api.login("change@example.com")
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        response = client.post(
            "/api/auth/change-password",
            json={"current_password": PASSWORD, "new_password": "BrandNewPassphrase42"},
            headers=headers,
        )
        assert response.status_code == 204, response.text
        # The access token was issued before the change: its password
        # fingerprint no longer matches.
        assert client.get("/api/auth/me", headers=headers).status_code == 401
        assert client.post(
            "/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code == 401
        assert client.post(
            "/api/auth/login", json={"email": "change@example.com", "password": "BrandNewPassphrase42"}
        ).status_code == 200


class TestSessionRevocation:
    def test_logout_kills_the_access_token_immediately(self, api, client):
        """A logged-out access token must not keep working for 15 minutes."""
        api.create_user("bye@example.com")
        tokens = api.login("bye@example.com")
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert client.get("/api/auth/me", headers=headers).status_code == 200

        assert client.post(
            "/api/auth/logout", json={"refresh_token": tokens["refresh_token"]}, headers=headers
        ).status_code == 204

        assert client.get("/api/auth/me", headers=headers).status_code == 401

    def test_logout_only_kills_that_session(self, api, client):
        api.create_user("two.devices@example.com")
        phone = api.login("two.devices@example.com")
        laptop = api.login("two.devices@example.com")
        phone_headers = {"Authorization": f"Bearer {phone['access_token']}"}
        laptop_headers = {"Authorization": f"Bearer {laptop['access_token']}"}

        client.post("/api/auth/logout", json={"refresh_token": phone["refresh_token"]}, headers=phone_headers)

        assert client.get("/api/auth/me", headers=phone_headers).status_code == 401
        assert client.get("/api/auth/me", headers=laptop_headers).status_code == 200

    def test_refresh_reuse_kills_the_access_tokens_of_that_session(self, api, client):
        api.create_user("stolen@example.com")
        first = api.login("stolen@example.com")
        second = client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]}).json()
        headers = {"Authorization": f"Bearer {second['access_token']}"}
        assert client.get("/api/auth/me", headers=headers).status_code == 200

        # The old token is replayed -> the whole family, including this session,
        # is revoked.
        client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]})
        assert client.get("/api/auth/me", headers=headers).status_code == 401
