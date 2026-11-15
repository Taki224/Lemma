from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

import yaml

ALLOWED_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "openrouter_api_key",
        "local_api_key",
        "zotero_api_key",
        "zotero_library_id",
        "zotero_library_type",
        "unpaywall_email",
        "github_token",
        "session_secret",
    }
)

_ENV_OVERRIDES: dict[str, str] = {
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "local_api_key": "LOCAL_API_KEY",
    "zotero_api_key": "ZOTERO_API_KEY",
    "zotero_library_id": "ZOTERO_LIBRARY_ID",
    "zotero_library_type": "ZOTERO_LIBRARY_TYPE",
    "unpaywall_email": "UNPAYWALL_EMAIL",
    "github_token": "GITHUB_TOKEN",
    "session_secret": "SESSION_SECRET",
}


class SecretsStore:
    """Manages /data/config/secrets.yaml with 0600 permissions and environment variable overrides."""

    def __init__(self, secrets_path: Path) -> None:
        self.secrets_path = Path(secrets_path)
        self._values: dict[str, str] = {}
        self._load_and_initialize()

    def _load_and_initialize(self) -> None:
        parent = self.secrets_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass

        loaded_data: dict[str, str] = {}
        if self.secrets_path.is_file():
            try:
                raw = yaml.safe_load(self.secrets_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        norm_k = str(k).strip().lower()
                        if norm_k in ALLOWED_SECRET_KEYS and v is not None and str(v).strip() != "":
                            loaded_data[norm_k] = str(v).strip()
            except yaml.YAMLError:
                loaded_data = {}

        self._values = loaded_data
        if not self._values.get("session_secret"):
            self._values["session_secret"] = secrets.token_hex(32)
        self._write_secrets_file()

    def _write_secrets_file(self) -> None:
        serialized = yaml.safe_dump(self._values, sort_keys=True)
        nonce = secrets.token_hex(8)
        tmp_path = self.secrets_path.with_name(f".{self.secrets_path.name}.{nonce}.tmp")
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
            os.replace(tmp_path, self.secrets_path)
            try:
                os.chmod(self.secrets_path, 0o600)
            except OSError:
                pass
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def get(self, key: str) -> str | None:
        norm_key = key.strip().lower()
        if norm_key not in ALLOWED_SECRET_KEYS:
            raise KeyError(f"Unknown secret key: {key!r}")

        # If a secret was explicitly updated or rotated in this process, prefer it
        stored_val = self._values.get(norm_key)

        env_name = _ENV_OVERRIDES.get(norm_key)
        if env_name:
            env_val = os.environ.get(env_name)
            if env_val is not None and env_val.strip() != "":
                # For session_secret, stored value wins if rotated/customized
                if norm_key == "session_secret" and stored_val:
                    return stored_val
                return env_val.strip()

        return stored_val

    def secret_source(self, key: str) -> str | None:
        """Where get() will read this secret from: environment, saved, or nowhere."""
        norm_key = key.strip().lower()
        if norm_key not in ALLOWED_SECRET_KEYS:
            raise KeyError(f"Unknown secret key: {key!r}")
        stored_val = self._values.get(norm_key)
        env_name = _ENV_OVERRIDES.get(norm_key)
        if env_name and norm_key != "session_secret":
            env_val = os.environ.get(env_name)
            if env_val is not None and env_val.strip() != "":
                return "environment"
        if stored_val:
            return "saved"
        return None

    def update_secrets(self, updates: dict[str, Any]) -> None:
        for raw_key, raw_val in updates.items():
            norm_key = str(raw_key).strip().lower()
            if norm_key not in ALLOWED_SECRET_KEYS:
                continue
            if raw_val is None or str(raw_val).strip() == "":
                if norm_key != "session_secret":
                    self._values.pop(norm_key, None)
            else:
                self._values[norm_key] = str(raw_val).strip()
        if not self._values.get("session_secret"):
            self._values["session_secret"] = secrets.token_hex(32)
        self._write_secrets_file()

    def rotate_session_secret(self, new_secret: str | None = None) -> str:
        if new_secret is not None and new_secret.strip():
            cleaned = new_secret.strip()
            if len(cleaned) < 32:
                raise ValueError("Session secret must be at least 32 characters long")
            secret_val = cleaned
        else:
            secret_val = secrets.token_hex(32)
        self._values["session_secret"] = secret_val
        self._write_secrets_file()
        return secret_val

    def get_masked_status(self) -> dict[str, bool]:
        """Return strictly boolean presence indicators for each secret key."""
        return {
            "has_openrouter_key": bool(self.get("openrouter_api_key")),
            "has_zotero_key": bool(self.get("zotero_api_key")),
            "has_zotero_library_id": bool(self.get("zotero_library_id")),
            "has_unpaywall_email": bool(self.get("unpaywall_email")),
            "has_github_token": bool(self.get("github_token")),
            "has_session_secret": bool(self.get("session_secret")),
        }

    def get_redactable_values(self) -> list[str]:
        """Return non-empty secret strings (internal use only for log redaction)."""
        values: list[str] = []
        for key in ALLOWED_SECRET_KEYS:
            val = self.get(key)
            if val and len(val) >= 6:
                values.append(val)
        return values
