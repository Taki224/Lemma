from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_HASHER = PasswordHasher()
_DUMMY_HASH = _HASHER.hash("dummy-constant-time-fallback-password")


class PasswordVerifier:
    """Verifies static workspace password using Argon2id, hashing plaintext on startup."""

    def __init__(
        self,
        raw_or_hashed_password: str | None = None,
        settings_yaml_path: Path | None = None,
    ) -> None:
        self.settings_yaml_path = Path(settings_yaml_path) if settings_yaml_path else None
        self.password_hash: str | None = None
        self._initialize_password(raw_or_hashed_password)

    def _initialize_password(self, raw_or_hashed: str | None) -> None:
        candidate = raw_or_hashed
        yaml_data: dict[str, Any] = {}

        if self.settings_yaml_path and self.settings_yaml_path.is_file():
            try:
                loaded = yaml.safe_load(self.settings_yaml_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    yaml_data = loaded
            except yaml.YAMLError:
                yaml_data = {}

        if not candidate:
            env_pw = os.environ.get("WORKSPACE_PASSWORD")
            if env_pw and env_pw.strip():
                candidate = env_pw.strip()
            elif isinstance(yaml_data.get("workspace_password_hash"), str):
                candidate = yaml_data["workspace_password_hash"].strip()
            elif isinstance(yaml_data.get("workspace_password"), str):
                candidate = yaml_data["workspace_password"].strip()

        if not candidate:
            candidate = "admin"

        if candidate.startswith("$argon2"):
            self.password_hash = candidate
        else:
            self.password_hash = _HASHER.hash(candidate)

        if self.settings_yaml_path:
            needs_write = (
                yaml_data.get("workspace_password_hash") != self.password_hash
                or "workspace_password" in yaml_data
            )
            if needs_write:
                yaml_data.pop("workspace_password", None)
                yaml_data["workspace_password_hash"] = self.password_hash
                try:
                    self.settings_yaml_path.parent.mkdir(parents=True, exist_ok=True)
                    self.settings_yaml_path.write_text(
                        yaml.safe_dump(yaml_data, sort_keys=False),
                        encoding="utf-8",
                    )
                except OSError:
                    pass

    def verify_password(self, candidate: str) -> bool:
        if not candidate or not self.password_hash:
            try:
                _HASHER.verify(_DUMMY_HASH, candidate or "")
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                pass
            return False

        try:
            return bool(_HASHER.verify(self.password_hash, candidate))
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


def resolve_client_ip(request: Any, trusted_proxies: list[str]) -> str:
    """Resolve client IP, honoring X-Forwarded-For only when immediate peer is in trusted_proxies."""
    peer_ip = "unknown"
    client_obj = getattr(request, "client", None)
    if client_obj is not None and getattr(client_obj, "host", None):
        peer_ip = str(client_obj.host).strip()

    trusted_set = {ip.strip() for ip in trusted_proxies if ip.strip()}
    if peer_ip not in trusted_set:
        return peer_ip

    headers = getattr(request, "headers", {}) or {}
    xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if not xff:
        return peer_ip

    hops = [part.strip() for part in str(xff).split(",") if part.strip()]
    if not hops:
        return peer_ip

    for hop in reversed(hops):
        if hop not in trusted_set:
            return hop
    return hops[0]


@dataclass(slots=True)
class _BucketState:
    failures: list[float] = field(default_factory=list)
    blocked_until: float = 0.0


class LoginRateLimiter:
    """Sliding-window brute-force login limiter with exponential backoff after 5 failures."""

    def __init__(
        self,
        max_free_failures: int = 5,
        window_seconds: float = 900.0,
        max_backoff_seconds: int = 300,
        global_max_failures: int = 50,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_free_failures = max_free_failures
        self.window_seconds = window_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.global_max_failures = global_max_failures
        self._time_fn = time_fn
        self._buckets: dict[str, _BucketState] = {}
        self._global_bucket = _BucketState()

    def _prune(self, bucket: _BucketState, now: float) -> None:
        cutoff = now - self.window_seconds
        bucket.failures = [ts for ts in bucket.failures if ts >= cutoff]

    def check_allowed(self, ip: str) -> tuple[bool, int]:
        now = self._time_fn()
        self._prune(self._global_bucket, now)
        if now < self._global_bucket.blocked_until:
            retry = max(1, int(round(self._global_bucket.blocked_until - now)))
            return False, retry

        bucket = self._buckets.get(ip)
        if bucket is None:
            return True, 0

        self._prune(bucket, now)
        if now < bucket.blocked_until:
            retry = max(1, int(round(bucket.blocked_until - now)))
            return False, retry

        return True, 0

    def record_result(self, ip: str, success: bool) -> None:
        now = self._time_fn()
        if success:
            self._buckets.pop(ip, None)
            return

        bucket = self._buckets.setdefault(ip, _BucketState())
        self._prune(bucket, now)
        bucket.failures.append(now)

        fail_count = len(bucket.failures)
        if fail_count >= self.max_free_failures:
            exponent = fail_count - self.max_free_failures
            backoff = min(self.max_backoff_seconds, int(2**exponent))
            bucket.blocked_until = now + float(max(1, backoff))

        self._prune(self._global_bucket, now)
        self._global_bucket.failures.append(now)
        if len(self._global_bucket.failures) >= self.global_max_failures:
            g_exp = len(self._global_bucket.failures) - self.global_max_failures
            g_backoff = min(self.max_backoff_seconds, int(2**g_exp))
            self._global_bucket.blocked_until = now + float(max(1, g_backoff))


class SecretRedactingFilter(logging.Filter):
    """Logging filter that scrubs configured secrets, passwords, and tokens from log records."""

    def __init__(self, secrets: Iterable[str] | None = None) -> None:
        super().__init__()
        self._secrets: set[str] = set()
        if secrets:
            self.register_secrets(secrets)

    def register_secrets(self, secrets: Iterable[str]) -> None:
        for item in secrets:
            if item and isinstance(item, str) and len(item.strip()) >= 4:
                self._secrets.add(item.strip())

    def _scrub_text(self, text: str) -> str:
        scrubbed = text
        for secret in self._secrets:
            if secret in scrubbed:
                scrubbed = scrubbed.replace(secret, "[REDACTED]")
        return scrubbed

    def _scrub_value(self, val: Any) -> Any:
        if isinstance(val, str):
            return self._scrub_text(val)
        if isinstance(val, tuple):
            return tuple(self._scrub_value(v) for v in val)
        if isinstance(val, list):
            return [self._scrub_value(v) for v in val]
        if isinstance(val, dict):
            return {k: self._scrub_value(v) for k, v in val.items()}
        return val

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        if isinstance(record.msg, str):
            record.msg = self._scrub_text(record.msg)
        if record.args:
            record.args = self._scrub_value(record.args)
        return True
