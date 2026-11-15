from __future__ import annotations

import io
import logging
from pathlib import Path
from types import SimpleNamespace

import yaml

from app.auth.security import (
    LoginRateLimiter,
    PasswordVerifier,
    SecretRedactingFilter,
    resolve_client_ip,
)


def test_plaintext_password_hashed_with_argon2_on_init(tmp_path: Path) -> None:
    settings_yaml = tmp_path / "settings.yaml"
    settings_yaml.write_text(
        yaml.safe_dump({"workspace_password_hash": "my-plaintext-password-123"}),
        encoding="utf-8",
    )

    verifier = PasswordVerifier(
        raw_or_hashed_password="my-plaintext-password-123",
        settings_yaml_path=settings_yaml,
    )

    assert verifier.password_hash is not None
    assert verifier.password_hash.startswith("$argon2id$")
    assert "my-plaintext-password-123" not in verifier.password_hash

    persisted = yaml.safe_load(settings_yaml.read_text(encoding="utf-8"))
    assert persisted["workspace_password_hash"].startswith("$argon2id$")
    assert "my-plaintext-password-123" not in settings_yaml.read_text(encoding="utf-8")

    assert verifier.verify_password("my-plaintext-password-123") is True
    assert verifier.verify_password("wrong-password") is False
    assert verifier.verify_password("") is False


def _fake_request(peer_ip: str, xff: str | None = None):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return SimpleNamespace(
        client=SimpleNamespace(host=peer_ip),
        headers=headers,
    )


def test_rate_limiter_enforces_exponential_backoff_and_ignores_spoofed_xff_from_untrusted_ip() -> None:
    trusted_proxies = ["127.0.0.1", "::1"]

    # Untrusted direct peer spoofing X-Forwarded-For must resolve to the peer's real IP
    req_spoofed = _fake_request(peer_ip="203.0.113.50", xff="1.2.3.4")
    assert resolve_client_ip(req_spoofed, trusted_proxies) == "203.0.113.50"

    # Trusted local proxy forwarding X-Forwarded-For resolves to client IP
    req_trusted = _fake_request(peer_ip="127.0.0.1", xff="198.51.100.77, 127.0.0.1")
    assert resolve_client_ip(req_trusted, trusted_proxies) == "198.51.100.77"

    clock_now = 1000.0
    limiter = LoginRateLimiter(time_fn=lambda: clock_now)
    client_ip = resolve_client_ip(req_spoofed, trusted_proxies)

    # First 5 failed attempts are allowed before backoff kicks in
    for _ in range(5):
        allowed, retry_after = limiter.check_allowed(client_ip)
        assert allowed is True
        assert retry_after == 0
        limiter.record_result(client_ip, success=False)

    #After 5 failures, backoff is enforced (2^(5-5) = 1s or after 6th failure 2^(6-5) = 2s)
    limiter.record_result(client_ip, success=False)
    allowed_6th, retry_after_6th = limiter.check_allowed(client_ip)
    assert allowed_6th is False
    assert retry_after_6th >= 1

    # Spoofing a different X-Forwarded-For from the same untrusted peer IP still maps to 203.0.113.50
    req_spoofed_2 = _fake_request(peer_ip="203.0.113.50", xff="9.9.9.9")
    ip_2 = resolve_client_ip(req_spoofed_2, trusted_proxies)
    assert ip_2 == "203.0.113.50"
    assert limiter.check_allowed(ip_2)[0] is False

    # Advancing time past the backoff window allows retry, and success resets failure counter
    clock_now += 10.0
    assert limiter.check_allowed(client_ip) == (True, 0)
    limiter.record_result(client_ip, success=True)
    assert limiter.check_allowed(client_ip) == (True, 0)


def test_secret_redacting_filter_scrubs_keys_from_logs() -> None:
    redactor = SecretRedactingFilter(
        secrets=["sk-or-v1-999888777", "zot-secret-token-555", "my-session-hmac-key"]
    )
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(redactor)

    logger = logging.getLogger("test_redactor_logger")
    logger.setLevel(logging.INFO)
    logger.handlers = [handler]
    logger.propagate = False

    logger.info(
        "Calling OpenRouter with key=%s and zotero=%s",
        "sk-or-v1-999888777",
        "zot-secret-token-555",
    )
    output = stream.getvalue()
    assert "sk-or-v1-999888777" not in output
    assert "zot-secret-token-555" not in output
    assert "[REDACTED]" in output
