from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.auth.session import SESSION_COOKIE_NAME
from app.main import create_app


def test_login_logout_revokes_replayed_cookie_and_updates_secrets_and_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("WORKSPACE_PASSWORD", "correct-horse-battery")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    app = create_app()
    with TestClient(app) as client:
        # 1. Check status before login -> unauthenticated
        r_status_0 = client.get("/api/auth/status")
        assert r_status_0.status_code == 200
        status_0 = r_status_0.json()
        assert status_0["authenticated"] is False
        assert status_0["csrf_token"] is None
        assert status_0["privacy_consent_accepted"] is False

        # 2. Login with wrong password -> 401
        r_bad = client.post("/api/auth/login", json={"password": "wrong-password"})
        assert r_bad.status_code == 401

        # 3. Login with valid password -> 200 + session cookie + csrf_token
        r_login = client.post(
            "/api/auth/login",
            json={"password": "correct-horse-battery"},
        )
        assert r_login.status_code == 200
        login_payload = r_login.json()
        assert login_payload["authenticated"] is True
        csrf_token = login_payload["csrf_token"]
        assert csrf_token
        assert login_payload["privacy_consent_accepted"] is False

        captured_cookie = client.cookies.get(SESSION_COOKIE_NAME)
        assert captured_cookie

        # 4. Update secrets via PUT /api/settings/secrets (with CSRF)
        r_secrets = client.put(
            "/api/settings/secrets",
            json={
                "openrouter_api_key": "sk-or-v1-updated-key",
                "zotero_api_key": "zot-updated-key",
                "unpaywall_email": "me@example.edu",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r_secrets.status_code == 200
        secrets_payload = r_secrets.json()
        assert secrets_payload["secrets"]["has_openrouter_key"] is True
        assert secrets_payload["secrets"]["has_zotero_key"] is True
        assert secrets_payload["secrets"]["has_unpaywall_email"] is True
        assert "sk-or-v1-updated-key" not in r_secrets.text

        # 5. Record privacy consent via POST /api/settings/consent
        r_consent = client.post(
            "/api/settings/consent",
            json={"accepted": True},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r_consent.status_code == 200
        assert r_consent.json()["privacy_consent_accepted"] is True

        persisted_settings = yaml.safe_load(
            (data_dir / "config" / "settings.yaml").read_text(encoding="utf-8")
        )
        assert persisted_settings["privacy_consent_accepted"] is True

        # 6. Logout via POST /api/auth/logout -> revokes session in SQLite
        r_logout = client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r_logout.status_code == 200

        # 7. Replay the old session cookie on a protected endpoint -> must return 401
        client.cookies.set(SESSION_COOKIE_NAME, captured_cookie)
        r_replay = client.post(
            "/api/settings/consent",
            json={"accepted": True},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r_replay.status_code == 401


def test_login_rate_limit_returns_429(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("WORKSPACE_PASSWORD", "real-password")

    app = create_app()
    with TestClient(app) as client:
        for attempt in range(1, 6):
            resp = client.post("/api/auth/login", json={"password": f"bad-{attempt}"})
            assert resp.status_code == 401

        # 6th attempt is rate-limited with 429 Too Many Requests and Retry-After header
        resp_6 = client.post("/api/auth/login", json={"password": "bad-6"})
        assert resp_6.status_code == 429
        assert "Retry-After" in resp_6.headers or "retry-after" in resp_6.headers
        assert int(resp_6.headers["retry-after"]) >= 1


def test_non_localhost_plain_http_sets_warning_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("WORKSPACE_PASSWORD", "test-pass")

    app = create_app()
    with TestClient(app) as client:
        r_local = client.get("/api/auth/status", headers={"Host": "localhost:8080"})
        assert r_local.status_code == 200
        assert r_local.json()["insecure_http_warning"] is False

        r_lan = client.get("/api/auth/status", headers={"Host": "192.168.1.50:8080"})
        assert r_lan.status_code == 200
        assert r_lan.json()["insecure_http_warning"] is True
