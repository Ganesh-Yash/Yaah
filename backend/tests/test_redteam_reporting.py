"""The attack script must not report an intermediary's answer as the app's.

Found on a real deployment: two injection checks returned HTTP 403 behind Render's
edge filter. The app itself answers 201 and 404 for the same payloads (verified
against a prod-mode instance), which means the requests never arrived — but the
report called them FAIL, blaming the application for a control it never exercised.
"""

from __future__ import annotations

import httpx

from scripts.redteam import is_our_error_envelope


def test_our_error_envelope_is_recognised():
    response = httpx.Response(
        404,
        json={"error": {"code": "NOT_FOUND", "message": "Account not found.",
                        "details": {}, "request_id": "req_abc_1"}},
    )
    assert is_our_error_envelope(response) is True


def test_success_payload_from_the_app_is_recognised():
    response = httpx.Response(201, json={"transaction": {"id": "t1"}, "replayed": False})
    assert is_our_error_envelope(response) is True


def test_a_proxy_error_page_is_not_our_envelope():
    """A WAF's 403 is HTML or a bare status, not this API's JSON."""
    assert is_our_error_envelope(httpx.Response(403, text="<html>Forbidden</html>")) is False
    assert is_our_error_envelope(httpx.Response(403, text="Forbidden")) is False
    assert is_our_error_envelope(httpx.Response(403, json=["not", "an", "object"])) is False
