from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request, Response, WebSocket, status

from app.config_secrets import SecretsStore
from app.db import DatabaseManager

SESSION_COOKIE_NAME = "lemma_session"
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_LOCALHOST_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    csrf_token: str
    issued_at: int
    last_active: int
    token: str


class SessionManager:
    """SQLite-backed HMAC-SHA256 session cookie manager with server-side revocation and CSRF tokens."""

    def __init__(
        self,
        db: DatabaseManager,
        secrets: SecretsStore,
        idle_timeout_sec: int = 43200,
        trusted_proxy_ips: list[str] | None = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.secrets = secrets
        self.idle_timeout_sec = idle_timeout_sec
        self.trusted_proxy_ips = (
            list(trusted_proxy_ips)
            if trusted_proxy_ips is not None
            else ["127.0.0.1", "::1"]
        )
        self._time_fn = time_fn

    def _now(self) -> int:
        return int(self._time_fn())

    def _signing_key(self) -> bytes:
        sec = self.secrets.get("session_secret")
        if not sec:
            sec = self.secrets.rotate_session_secret()
        return sec.encode("utf-8")

    def _sign_payload(self, payload: str) -> str:
        return hmac.new(
            self._signing_key(),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def create_session(self) -> SessionRecord:
        session_id = secrets.token_urlsafe(24)
        csrf_token = secrets.token_urlsafe(24)
        now = self._now()
        payload = f"{session_id}.{now}.{csrf_token}"
        sig = self._sign_payload(payload)
        token = f"{payload}.{sig}"

        with closing(self.db.get_connection()) as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, csrf_token, issued_at, last_active, revoked)
                VALUES (?, ?, ?, ?, 0);
                """,
                (session_id, csrf_token, now, now),
            )

        return SessionRecord(
            session_id=session_id,
            csrf_token=csrf_token,
            issued_at=now,
            last_active=now,
            token=token,
        )

    def validate_token(self, token: str | None) -> SessionRecord | None:
        if not token or not isinstance(token, str):
            return None
        parts = token.split(".")
        if len(parts) != 4:
            return None

        session_id, issued_str, csrf_token, provided_sig = parts
        if not session_id or not csrf_token or not issued_str.isdigit():
            return None

        payload = f"{session_id}.{issued_str}.{csrf_token}"
        expected_sig = self._sign_payload(payload)
        if not hmac.compare_digest(provided_sig, expected_sig):
            return None

        now = self._now()
        with closing(self.db.get_connection()) as conn:
            row = conn.execute(
                """
                SELECT session_id, csrf_token, issued_at, last_active, revoked
                FROM sessions
                WHERE session_id = ?;
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            if int(row["revoked"]) != 0:
                return None
            if not hmac.compare_digest(str(row["csrf_token"]), csrf_token):
                return None

            last_active = int(row["last_active"])
            if now - last_active > self.idle_timeout_sec:
                conn.execute(
                    "UPDATE sessions SET revoked = 1 WHERE session_id = ?;",
                    (session_id,),
                )
                return None

            conn.execute(
                "UPDATE sessions SET last_active = ? WHERE session_id = ?;",
                (now, session_id),
            )

        return SessionRecord(
            session_id=session_id,
            csrf_token=csrf_token,
            issued_at=int(row["issued_at"]),
            last_active=now,
            token=token,
        )

    def revoke_session(self, session_id: str) -> None:
        with closing(self.db.get_connection()) as conn:
            conn.execute(
                "UPDATE sessions SET revoked = 1 WHERE session_id = ?;",
                (session_id,),
            )

    def rotate_secret(self, new_secret: str | None = None) -> str:
        rotated = self.secrets.rotate_session_secret(new_secret)
        with closing(self.db.get_connection()) as conn:
            conn.execute("UPDATE sessions SET revoked = 1;")
        return rotated

    def is_secure_request(self, request: Any) -> bool:
        url_obj = getattr(request, "url", None)
        if url_obj is not None and getattr(url_obj, "scheme", "").lower() == "https":
            return True

        client_obj = getattr(request, "client", None)
        peer_ip = str(getattr(client_obj, "host", "")).strip() if client_obj else ""
        trusted_set = {ip.strip() for ip in self.trusted_proxy_ips if ip.strip()}
        if peer_ip in trusted_set:
            headers = getattr(request, "headers", {}) or {}
            xfp = headers.get("x-forwarded-proto") or headers.get("X-Forwarded-Proto")
            if xfp and str(xfp).split(",")[0].strip().lower() == "https":
                return True
        return False

    def is_insecure_non_localhost_http(self, request: Any) -> bool:
        if self.is_secure_request(request):
            return False
        headers = getattr(request, "headers", {}) or {}
        host_header = str(headers.get("host") or headers.get("Host") or "").strip().lower()
        if not host_header:
            url_obj = getattr(request, "url", None)
            host_header = str(getattr(url_obj, "hostname", "") or "").strip().lower()
        if host_header.startswith("[") and "]" in host_header:
            host_only = host_header[: host_header.index("]") + 1]
        else:
            host_only = host_header.split(":")[0]
        if not host_only or host_only in _LOCALHOST_HOSTS:
            return False
        return True

    def set_session_cookie(self, response: Response, token: str, request: Any) -> None:
        secure = self.is_secure_request(request)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            httponly=True,
            samesite="strict",
            secure=secure,
            max_age=self.idle_timeout_sec,
            path="/",
        )

    def clear_session_cookie(self, response: Response, request: Any) -> None:
        secure = self.is_secure_request(request)
        response.delete_cookie(
            key=SESSION_COOKIE_NAME,
            httponly=True,
            samesite="strict",
            secure=secure,
            path="/",
        )


def _get_session_manager(request: Request) -> SessionManager:
    manager: SessionManager | None = getattr(request.app.state, "session_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SessionManager is not initialized on application state.",
        )
    return manager


async def require_authenticated_session(request: Request) -> SessionRecord:
    """FastAPI dependency enforcing an active, unrevoked SQLite session cookie."""
    cached: SessionRecord | None = getattr(request.state, "session", None)
    if cached is not None:
        return cached

    manager = _get_session_manager(request)
    raw_cookie = request.cookies.get(SESSION_COOKIE_NAME)
    session_record = manager.validate_token(raw_cookie)
    if session_record is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required or session expired.",
        )
    request.state.session = session_record
    return session_record


async def require_csrf(
    request: Request,
    session: SessionRecord = Depends(require_authenticated_session),
) -> None:
    """FastAPI dependency enforcing X-CSRF-Token on mutating HTTP requests."""
    if request.method.upper() not in _MUTATING_METHODS:
        return
    header_token = request.headers.get("X-CSRF-Token") or request.headers.get("x-csrf-token")
    if not header_token or not hmac.compare_digest(header_token.strip(), session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing or invalid CSRF token.",
        )


async def authenticate_websocket(websocket: WebSocket) -> bool:
    """Validate the session cookie on an incoming WebSocket handshake."""
    manager: SessionManager | None = getattr(websocket.app.state, "session_manager", None)
    if manager is None:
        return False
    raw_cookie = websocket.cookies.get(SESSION_COOKIE_NAME)
    session_record = manager.validate_token(raw_cookie)
    if session_record is None:
        return False
    websocket.state.session = session_record
    return True
