from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.auth.session import (
    SESSION_COOKIE_NAME,
    SessionManager,
    require_authenticated_session,
    require_csrf,
)
from app.config_secrets import SecretsStore
from app.db import DatabaseManager


def _make_fake_request(scheme: str = "http", peer_ip: str = "127.0.0.1", xfp: str | None = None):
    headers = {}
    if xfp is not None:
        headers["x-forwarded-proto"] = xfp
    return SimpleNamespace(
        url=SimpleNamespace(scheme=scheme),
        client=SimpleNamespace(host=peer_ip),
        headers=headers,
    )


def test_session_cookie_flags_idle_expiry_and_server_side_logout_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    db = DatabaseManager(db_path=tmp_path / "app.db", backup_dir=tmp_path / "backups")
    db.initialize()
    secrets = SecretsStore(secrets_path=tmp_path / "config" / "secrets.yaml")

    clock_now = 1_700_000_000
    manager = SessionManager(
        db=db,
        secrets=secrets,
        idle_timeout_sec=600,
        trusted_proxy_ips=["127.0.0.1", "::1"],
        time_fn=lambda: clock_now,
    )

    session_data = manager.create_session()
    assert session_data.session_id
    assert session_data.csrf_token
    assert session_data.token

    # Cookie flags over plain HTTP vs trusted HTTPS proxy vs untrusted spoofed X-Forwarded-Proto
    http_req = _make_fake_request(scheme="http", peer_ip="127.0.0.1")
    response_http = JSONResponse({"ok": True})
    manager.set_session_cookie(response_http, session_data.token, http_req)
    set_cookie_http = response_http.headers["set-cookie"].lower()
    assert "httponly" in set_cookie_http
    assert "samesite=strict" in set_cookie_http
    assert "secure" not in set_cookie_http

    https_proxy_req = _make_fake_request(scheme="http", peer_ip="127.0.0.1", xfp="https")
    response_https = JSONResponse({"ok": True})
    manager.set_session_cookie(response_https, session_data.token, https_proxy_req)
    set_cookie_https = response_https.headers["set-cookie"].lower()
    assert "secure" in set_cookie_https

    spoofed_https_req = _make_fake_request(scheme="http", peer_ip="203.0.113.99", xfp="https")
    response_spoofed = JSONResponse({"ok": True})
    manager.set_session_cookie(response_spoofed, session_data.token, spoofed_https_req)
    assert "secure" not in response_spoofed.headers["set-cookie"].lower()

    # Valid within idle timeout, updates last_active
    clock_now += 300
    validated = manager.validate_token(session_data.token)
    assert validated is not None
    assert validated.session_id == session_data.session_id

    # Advance another 300s (total 600s since issue, but only 300s since last_active) -> still valid
    clock_now += 300
    assert manager.validate_token(session_data.token) is not None

    # Advance past idle_timeout_sec (601s without activity) -> expired
    clock_now += 601
    assert manager.validate_token(session_data.token) is None

    # Create a second session and revoke it explicitly via revoke_session()
    session_2 = manager.create_session()
    assert manager.validate_token(session_2.token) is not None
    manager.revoke_session(session_2.session_id)
    # Replaying the cookie after server-side logout revocation must fail immediately
    assert manager.validate_token(session_2.token) is None


def test_secret_rotation_persists_to_secrets_store_and_revokes_prior_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    db = DatabaseManager(db_path=tmp_path / "app.db", backup_dir=tmp_path / "backups")
    db.initialize()
    secrets_path = tmp_path / "config" / "secrets.yaml"
    secrets = SecretsStore(secrets_path=secrets_path)

    old_secret = secrets.get("session_secret")
    manager = SessionManager(db=db, secrets=secrets)

    s1 = manager.create_session()
    assert manager.validate_token(s1.token) is not None

    manager.rotate_secret()

    new_secret = secrets.get("session_secret")
    assert new_secret != old_secret
    # Verify persisted to disk by reloading SecretsStore
    reloaded = SecretsStore(secrets_path=secrets_path)
    assert reloaded.get("session_secret") == new_secret

    # Old token is rejected
    assert manager.validate_token(s1.token) is None

    # New session created with rotated secret works
    s2 = manager.create_session()
    assert manager.validate_token(s2.token) is not None


def test_csrf_guard_blocks_post_without_matching_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    db = DatabaseManager(db_path=tmp_path / "app.db", backup_dir=tmp_path / "backups")
    db.initialize()
    secrets = SecretsStore(secrets_path=tmp_path / "config" / "secrets.yaml")
    manager = SessionManager(db=db, secrets=secrets)

    app = FastAPI()
    app.state.session_manager = manager

    @app.post(
        "/api/protected-mutation",
        dependencies=[Depends(require_authenticated_session), Depends(require_csrf)],
    )
    async def protected_mutation():
        return {"mutated": True}

    session_data = manager.create_session()

    with TestClient(app) as client:
        # Unauthenticated request -> 401
        r_unauth = client.post("/api/protected-mutation")
        assert r_unauth.status_code == 401

        # Authenticated cookie but missing X-CSRF-Token -> 403
        client.cookies.set(SESSION_COOKIE_NAME, session_data.token)
        r_no_csrf = client.post("/api/protected-mutation")
        assert r_no_csrf.status_code == 403

        # Mismatched X-CSRF-Token -> 403
        r_bad_csrf = client.post(
            "/api/protected-mutation",
            headers={"X-CSRF-Token": "wrong-csrf-token"},
        )
        assert r_bad_csrf.status_code == 403

        # Matching X-CSRF-Token -> 200
        r_ok = client.post(
            "/api/protected-mutation",
            headers={"X-CSRF-Token": session_data.csrf_token},
        )
        assert r_ok.status_code == 200
        assert r_ok.json() == {"mutated": True}
