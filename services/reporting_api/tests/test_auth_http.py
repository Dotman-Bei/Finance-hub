"""The token exchange, over HTTP, the way the dashboard performs it.

test_auth_and_reports.py asserts the permission matrix in Python. That is the
right test for the matrix and the wrong one for this: it proves an `Auditor`
principal cannot resolve exceptions, while saying nothing about whether a
browser can obtain a principal at all.

That gap had a cost. Every data endpoint is permission-guarded and
`REQUIRE_AUTH=true` is the compose default, yet the dashboard never called
`POST /auth/token` - `auth.setToken` existed in the client with no call sites.
The API tests passed because they mint tokens with `issue_token()` directly,
which is a path no browser can take. The result was a fully styled dashboard
that answered 401 on every panel.

These exercise the round trip end to end: refused without credentials, refused
with the wrong key, issued with the right one, and carrying the role that was
asked for.

The client is deliberately *not* used as a context manager. Entering it runs
the lifespan, which opens Redis connections for the metrics cache and the event
relay; the two endpoints here need neither, so staying outside it keeps the
test free of infrastructure.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.reporting_api.app import auth as auth_module
from services.reporting_api.app import main as main_module
from services.reporting_api.app.auth import Role

API_KEY = "test-service-key"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    """A gateway that enforces auth and knows one key.

    Both are module-level constants read from the environment at import time,
    so they are patched on the module rather than through env vars, which would
    have no effect after import.
    """
    monkeypatch.setattr(auth_module, "REQUIRE_AUTH", True)
    monkeypatch.setattr(main_module, "SERVICE_API_KEY", API_KEY)
    return TestClient(main_module.app)


def test_whoami_is_refused_without_a_token(client):
    """The state the dashboard starts in on a fresh browser."""
    assert client.get("/auth/me").status_code == 401


def test_a_guarded_endpoint_is_refused_without_a_token(client):
    """What every panel saw before the sign-in card existed."""
    assert client.get("/metrics/kpi").status_code == 401


def test_token_issue_rejects_a_wrong_key(client):
    response = client.post(
        "/auth/token", json={"role": Role.FINANCE_MANAGER.value, "api_key": "wrong"}
    )
    assert response.status_code == 401


def test_token_issue_rejects_an_absent_key(client):
    response = client.post("/auth/token", json={"role": Role.FINANCE_MANAGER.value})
    assert response.status_code == 401


def test_token_issue_is_disabled_when_no_key_is_configured(client, monkeypatch):
    """Fail closed: an unset secret must not become an open door."""
    monkeypatch.setattr(main_module, "SERVICE_API_KEY", "")
    response = client.post(
        "/auth/token", json={"role": Role.FINANCE_MANAGER.value, "api_key": ""}
    )
    assert response.status_code == 503


@pytest.mark.parametrize("role", [r.value for r in Role])
def test_the_full_exchange_the_dashboard_performs(client, role):
    """Sign in, then use the token - for each of Sec. 3.4.1's three roles."""
    issued = client.post("/auth/token", json={"role": role, "api_key": API_KEY})
    assert issued.status_code == 200

    body = issued.json()
    assert body["token_type"] == "bearer"
    assert body["role"] == role
    assert body["access_token"]

    me = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200

    # The role the gateway enforces comes from inside the signed token, which
    # is why switching role in the UI has to mint a new one rather than
    # relabelling the request.
    assert me.json()["role"] == role


def test_a_forged_bearer_token_is_rejected(client):
    """`X-FinanceHub-Role` is not an authorisation, and neither is a made-up JWT."""
    response = client.get(
        "/auth/me",
        headers={
            "Authorization": "Bearer not-a-real-token",
            "X-FinanceHub-Role": Role.SYSTEM_ADMINISTRATOR.value,
        },
    )
    assert response.status_code == 401
