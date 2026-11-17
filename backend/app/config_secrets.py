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
        "zotero_key_name",
        "zotero_keys",
        "zotero_project_keys",
        "unpaywall_email",
        "github_token",
        "github_token_name",
        "github_tokens",
        "github_project_tokens",
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

_DICT_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "github_tokens",
        "zotero_keys",
        "github_project_tokens",
        "zotero_project_keys",
    }
)


class SecretsStore:
    """Manages /data/config/secrets.yaml with 0600 permissions and environment variable overrides."""

    def __init__(self, secrets_path: Path) -> None:
        self.secrets_path = Path(secrets_path)
        self._values: dict[str, str] = {}
        self._github_tokens: dict[str, str] = {}
        self._zotero_keys: dict[str, str] = {}
        self._github_project_tokens: dict[str, str] = {}
        self._zotero_project_keys: dict[str, str] = {}
        self._load_and_initialize()

    def _load_and_initialize(self) -> None:
        parent = self.secrets_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass

        loaded_data: dict[str, str] = {}
        loaded_dicts: dict[str, dict[str, str]] = {k: {} for k in _DICT_SECRET_KEYS}
        if self.secrets_path.is_file():
            try:
                raw = yaml.safe_load(self.secrets_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        norm_k = str(k).strip().lower()
                        if norm_k in _DICT_SECRET_KEYS and isinstance(v, dict):
                            for sub_k, sub_v in v.items():
                                clean_sub_k = str(sub_k).strip()
                                clean_sub_v = str(sub_v).strip() if sub_v is not None else ""
                                if clean_sub_k and clean_sub_v:
                                    loaded_dicts[norm_k][clean_sub_k] = clean_sub_v
                        elif norm_k in ALLOWED_SECRET_KEYS and norm_k not in _DICT_SECRET_KEYS and v is not None and str(v).strip() != "":
                            loaded_data[norm_k] = str(v).strip()
            except yaml.YAMLError:
                loaded_data = {}

        self._values = loaded_data
        self._github_tokens = loaded_dicts["github_tokens"]
        self._zotero_keys = loaded_dicts["zotero_keys"]
        self._github_project_tokens = loaded_dicts["github_project_tokens"]
        self._zotero_project_keys = loaded_dicts["zotero_project_keys"]
        if not self._values.get("session_secret"):
            self._values["session_secret"] = secrets.token_hex(32)
        self._write_secrets_file()

    def _write_secrets_file(self) -> None:
        payload: dict[str, Any] = dict(self._values)
        if self._github_tokens:
            payload["github_tokens"] = dict(self._github_tokens)
        if self._zotero_keys:
            payload["zotero_keys"] = dict(self._zotero_keys)
        if self._github_project_tokens:
            payload["github_project_tokens"] = dict(self._github_project_tokens)
        if self._zotero_project_keys:
            payload["zotero_project_keys"] = dict(self._zotero_project_keys)
        serialized = yaml.safe_dump(payload, sort_keys=True)
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

        if stored_val:
            return stored_val

        if norm_key == "github_token" and self._github_tokens:
            default_name = self._values.get("github_token_name")
            if default_name and default_name in self._github_tokens:
                return self._github_tokens[default_name]
            return next(iter(self._github_tokens.values()))
        if norm_key == "zotero_api_key" and self._zotero_keys:
            default_name = self._values.get("zotero_key_name")
            if default_name and default_name in self._zotero_keys:
                return self._zotero_keys[default_name]
            return next(iter(self._zotero_keys.values()))

        return None

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
        if norm_key == "github_token" and self._github_tokens:
            return "saved"
        if norm_key == "zotero_api_key" and self._zotero_keys:
            return "saved"
        return None

    def list_github_tokens(self) -> list[dict[str, str]]:
        return [{"name": name} for name in self._github_tokens]

    def list_zotero_keys(self) -> list[dict[str, str]]:
        return [{"name": name} for name in self._zotero_keys]

    def set_named_key(self, provider: str, name: str, value: str) -> None:
        clean_name = name.strip()
        clean_val = value.strip()
        if not clean_name:
            raise ValueError("Key name cannot be empty.")
        if not clean_val:
            raise ValueError("Key value cannot be empty.")
        norm_provider = provider.strip().lower()
        if norm_provider == "github":
            self._github_tokens[clean_name] = clean_val
            if not self._values.get("github_token_name") or self._values.get("github_token_name") not in self._github_tokens:
                self._values["github_token_name"] = clean_name
        elif norm_provider == "zotero":
            self._zotero_keys[clean_name] = clean_val
            if not self._values.get("zotero_key_name") or self._values.get("zotero_key_name") not in self._zotero_keys:
                self._values["zotero_key_name"] = clean_name
        else:
            raise ValueError(f"Unsupported key provider: {provider!r}")
        self._write_secrets_file()

    def remove_named_key(self, provider: str, name: str) -> bool:
        clean_name = name.strip()
        norm_provider = provider.strip().lower()
        if norm_provider == "github":
            removed = self._github_tokens.pop(clean_name, None) is not None
            if not removed:
                return False
            if self._values.get("github_token_name") == clean_name:
                if self._github_tokens:
                    self._values["github_token_name"] = next(iter(self._github_tokens))
                else:
                    self._values.pop("github_token_name", None)
            for proj_id in [pid for pid, kname in self._github_project_tokens.items() if kname == clean_name]:
                self._github_project_tokens.pop(proj_id, None)
        elif norm_provider == "zotero":
            removed = self._zotero_keys.pop(clean_name, None) is not None
            if not removed:
                return False
            if self._values.get("zotero_key_name") == clean_name:
                if self._zotero_keys:
                    self._values["zotero_key_name"] = next(iter(self._zotero_keys))
                else:
                    self._values.pop("zotero_key_name", None)
            for proj_id in [pid for pid, kname in self._zotero_project_keys.items() if kname == clean_name]:
                self._zotero_project_keys.pop(proj_id, None)
        else:
            raise ValueError(f"Unsupported key provider: {provider!r}")
        self._write_secrets_file()
        return True

    def get_github_token_for_project(
        self,
        project_id: str | None = None,
        token_name: str | None = None,
    ) -> tuple[str | None, str | None, str | None]:
        """Return (token, resolved_key_name, source)."""
        if token_name and token_name.strip():
            clean_name = token_name.strip()
            if clean_name in self._github_tokens:
                return self._github_tokens[clean_name], clean_name, "saved"
            return None, None, None
        if project_id and project_id in self._github_project_tokens:
            proj_name = self._github_project_tokens[project_id]
            if proj_name in self._github_tokens:
                return self._github_tokens[proj_name], proj_name, "saved"
        default_name = self._values.get("github_token_name")
        if default_name and default_name in self._github_tokens:
            return self._github_tokens[default_name], default_name, "saved"
        if self._github_tokens:
            first_name, first_tok = next(iter(self._github_tokens.items()))
            return first_tok, first_name, "saved"
        tok = self.get("github_token")
        src = self.secret_source("github_token")
        return tok, None, src

    def set_project_github_token_name(self, project_id: str, token_name: str | None) -> None:
        if token_name and token_name.strip():
            clean = token_name.strip()
            self._github_project_tokens[project_id] = clean
            self._values["github_token_name"] = clean
        else:
            self._github_project_tokens.pop(project_id, None)
        self._write_secrets_file()

    def get_zotero_key_for_project(
        self,
        project_id: str | None = None,
        key_name: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Return (api_key, resolved_key_name)."""
        if key_name and key_name.strip():
            clean_name = key_name.strip()
            if clean_name in self._zotero_keys:
                return self._zotero_keys[clean_name], clean_name
            return None, None
        if project_id and project_id in self._zotero_project_keys:
            proj_name = self._zotero_project_keys[project_id]
            if proj_name in self._zotero_keys:
                return self._zotero_keys[proj_name], proj_name
        default_name = self._values.get("zotero_key_name")
        if default_name and default_name in self._zotero_keys:
            return self._zotero_keys[default_name], default_name
        if self._zotero_keys:
            first_name, first_key = next(iter(self._zotero_keys.items()))
            return first_key, first_name
        return self.get("zotero_api_key"), None

    def set_project_zotero_key_name(self, project_id: str | None, key_name: str | None) -> None:
        if key_name and key_name.strip():
            clean = key_name.strip()
            if project_id:
                self._zotero_project_keys[project_id] = clean
            self._values["zotero_key_name"] = clean
        elif project_id:
            self._zotero_project_keys.pop(project_id, None)
        self._write_secrets_file()

    def update_secrets(self, updates: dict[str, Any]) -> None:
        for raw_key, raw_val in updates.items():
            norm_key = str(raw_key).strip().lower()
            if norm_key not in ALLOWED_SECRET_KEYS or norm_key in _DICT_SECRET_KEYS:
                continue
            if raw_val is None or str(raw_val).strip() == "":
                if norm_key != "session_secret":
                    self._values.pop(norm_key, None)
                if norm_key == "zotero_api_key":
                    self._values.pop("zotero_key_name", None)
                    self._zotero_keys.clear()
                    self._zotero_project_keys.clear()
                elif norm_key == "github_token":
                    self._values.pop("github_token_name", None)
                    self._github_project_tokens.clear()
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
            if key in _DICT_SECRET_KEYS or key in {"github_token_name", "zotero_key_name"}:
                continue
            val = self.get(key)
            if val and len(val) >= 6:
                values.append(val)
        for val in (*self._github_tokens.values(), *self._zotero_keys.values()):
            if val and len(val) >= 6 and val not in values:
                values.append(val)
        return values
