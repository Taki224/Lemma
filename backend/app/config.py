from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ConfigPermissionError(PermissionError):
    """Raised when the /data persistence volume is not writable by the application user."""


def _parse_bool_env(val: str) -> bool:
    normalized = val.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean environment value: {val!r}")


def _parse_list_env(val: str) -> list[str]:
    stripped = val.strip()
    if not stripped:
        return []
    if stripped.startswith("["):
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in stripped.split(",") if item.strip()]


class AppSettings(BaseModel):
    """Typed application settings loaded from /data/config/settings.yaml and environment variables."""

    model_config = ConfigDict(validate_assignment=True)

    data_dir: Path = Field(default=Path("/data"))
    workspace_password: str | None = Field(default=None)
    session_secret: str | None = Field(default=None)
    session_idle_timeout_seconds: int = Field(default=43200, ge=60)
    compile_timeout_seconds: int = Field(default=60, ge=1)
    compile_memory_limit_mb: int = Field(default=1024, ge=64)
    overlap_ngram_words: int = Field(default=8, ge=3)
    candidate_ttl_days: int = Field(default=14, ge=1)
    snapshot_compact_threshold: int = Field(default=100, ge=5)
    request_spend_cap_usd: float = Field(default=1.50, ge=0.0)
    daily_spend_cap_usd: float = Field(default=10.00, ge=0.0)
    review_confirm_usd: float = Field(default=0.50, ge=0.0)
    model_allowlist: list[str] = Field(default_factory=list)
    trusted_proxy_ips: list[str] = Field(default_factory=lambda: ["127.0.0.1", "::1"])
    zotero_push_enabled: bool = Field(default=False)
    privacy_consent_accepted: bool = Field(default=False)
    zdr_enabled: bool = Field(default=True)
    smart_routing_enabled: bool = Field(default=False)
    smart_routing_model: str = Field(default="")
    chat_naming_enabled: bool = Field(default=False)
    chat_naming_model: str = Field(default="")
    web_search_enabled: bool = Field(default=False)
    apply_patch_enabled: bool = Field(default=False)
    local_model_base_url: str = Field(default="http://localhost:11434/v1")

    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def texmf_dir(self) -> Path:
        return self.data_dir / "texmf"

    @property
    def scholarly_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "scholarly"

    @property
    def zotero_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "zotero"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def settings_yaml_path(self) -> Path:
        return self.config_dir / "settings.yaml"

    @property
    def secrets_yaml_path(self) -> Path:
        return self.config_dir / "secrets.yaml"

    @property
    def prompts_yaml_path(self) -> Path:
        return self.config_dir / "prompts.yaml"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "app.db"

    def to_persisted_yaml_dict(self) -> dict[str, Any]:
        """Serialize non-secret settings suitable for /data/config/settings.yaml."""
        data: dict[str, Any] = {
            "privacy_consent_accepted": self.privacy_consent_accepted,
            "session_idle_timeout_seconds": self.session_idle_timeout_seconds,
            "compile_timeout_seconds": self.compile_timeout_seconds,
            "compile_memory_limit_mb": self.compile_memory_limit_mb,
            "overlap_ngram_words": self.overlap_ngram_words,
            "candidate_ttl_days": self.candidate_ttl_days,
            "snapshot_compact_threshold": self.snapshot_compact_threshold,
            "request_spend_cap_usd": self.request_spend_cap_usd,
            "daily_spend_cap_usd": self.daily_spend_cap_usd,
            "review_confirm_usd": self.review_confirm_usd,
            "model_allowlist": list(self.model_allowlist),
            "trusted_proxy_ips": list(self.trusted_proxy_ips),
            "zotero_push_enabled": self.zotero_push_enabled,
            "zdr_enabled": self.zdr_enabled,
            "smart_routing_enabled": self.smart_routing_enabled,
            "smart_routing_model": self.smart_routing_model,
            "chat_naming_enabled": self.chat_naming_enabled,
            "chat_naming_model": self.chat_naming_model,
            "web_search_enabled": self.web_search_enabled,
            "apply_patch_enabled": self.apply_patch_enabled,
            "local_model_base_url": self.local_model_base_url,
        }
        return data


def _apply_env_overrides(merged: dict[str, Any]) -> None:
    int_mappings = {
        "SESSION_IDLE_TIMEOUT_SECONDS": "session_idle_timeout_seconds",
        "COMPILE_TIMEOUT_SECONDS": "compile_timeout_seconds",
        "COMPILE_MEMORY_LIMIT_MB": "compile_memory_limit_mb",
        "OVERLAP_NGRAM_WORDS": "overlap_ngram_words",
        "CANDIDATE_TTL_DAYS": "candidate_ttl_days",
        "SNAPSHOT_COMPACT_THRESHOLD": "snapshot_compact_threshold",
    }
    float_mappings = {
        "REQUEST_SPEND_CAP_USD": "request_spend_cap_usd",
        "DAILY_SPEND_CAP_USD": "daily_spend_cap_usd",
        "REVIEW_CONFIRM_USD": "review_confirm_usd",
    }
    str_mappings = {
        "WORKSPACE_PASSWORD": "workspace_password",
        "SESSION_SECRET": "session_secret",
        "SMART_ROUTING_MODEL": "smart_routing_model",
        "CHAT_NAMING_MODEL": "chat_naming_model",
        "LOCAL_MODEL_BASE_URL": "local_model_base_url",
    }
    list_mappings = {
        "MODEL_ALLOWLIST": "model_allowlist",
        "TRUSTED_PROXY_IPS": "trusted_proxy_ips",
    }
    bool_mappings = {
        "ZOTERO_PUSH_ENABLED": "zotero_push_enabled",
        "ZDR_ENABLED": "zdr_enabled",
        "SMART_ROUTING_ENABLED": "smart_routing_enabled",
        "CHAT_NAMING_ENABLED": "chat_naming_enabled",
        "WEB_SEARCH_ENABLED": "web_search_enabled",
        "APPLY_PATCH_ENABLED": "apply_patch_enabled",
    }

    for env_key, field_name in str_mappings.items():
        val = os.environ.get(env_key)
        if val is not None and val != "":
            merged[field_name] = val

    for env_key, field_name in int_mappings.items():
        val = os.environ.get(env_key)
        if val is not None and val.strip() != "":
            merged[field_name] = int(val.strip())

    for env_key, field_name in float_mappings.items():
        val = os.environ.get(env_key)
        if val is not None and val.strip() != "":
            merged[field_name] = float(val.strip())

    for env_key, field_name in list_mappings.items():
        val = os.environ.get(env_key)
        if val is not None:
            merged[field_name] = _parse_list_env(val)

    for env_key, field_name in bool_mappings.items():
        val = os.environ.get(env_key)
        if val is not None and val.strip() != "":
            merged[field_name] = _parse_bool_env(val)


_settings_save_lock = threading.Lock()


def save_settings_yaml(settings: AppSettings) -> None:
    """Persist non-secret configuration fields to /data/config/settings.yaml."""
    settings_path = settings.settings_yaml_path
    with _settings_save_lock:
        existing: dict[str, Any] = {}
        if settings_path.is_file():
            try:
                loaded = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing.update(loaded)
            except yaml.YAMLError:
                pass
        existing.update(settings.to_persisted_yaml_dict())
        settings_path.write_text(
            yaml.safe_dump(existing, sort_keys=False),
            encoding="utf-8",
        )


def load_dotenv_if_exists() -> None:
    """Load key-value pairs from .env or .env.local into os.environ if not already set."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    for env_name in (".env.local", ".env"):
        env_file = repo_root / env_name
        if env_file.is_file():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
            except OSError:
                pass


def bootstrap_data_dir(data_dir: Path | str | None = None) -> AppSettings:
    """Initialize data directory hierarchy, enforce permissions, and load AppSettings."""
    load_dotenv_if_exists()

    if data_dir is not None:
        target = Path(data_dir)
    elif os.environ.get("DATA_DIR"):
        target = Path(os.environ["DATA_DIR"])
    else:
        # Check if /data exists and is writable (e.g., container environment)
        container_data = Path("/data")
        try:
            if container_data.is_dir() and os.access(container_data, os.W_OK):
                target = container_data
            else:
                # Default to ./data relative to repository root for local development
                repo_root = Path(__file__).resolve().parent.parent.parent
                target = (repo_root / "data").resolve()
        except OSError:
            target = container_data

    resolved_dir = target.expanduser()

    try:
        resolved_dir.mkdir(parents=True, exist_ok=True)
        resolved_dir = resolved_dir.resolve()

        config_dir = resolved_dir / "config"
        projects_dir = resolved_dir / "projects"
        texmf_dir = resolved_dir / "texmf"
        scholarly_cache_dir = resolved_dir / "cache" / "scholarly"
        zotero_cache_dir = resolved_dir / "cache" / "zotero"
        backups_dir = resolved_dir / "backups"

        for directory in (
            config_dir,
            projects_dir,
            texmf_dir,
            scholarly_cache_dir,
            zotero_cache_dir,
            backups_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        os.chmod(config_dir, 0o700)
        os.chmod(projects_dir, 0o700)
        os.chmod(backups_dir, 0o700)
        os.chmod(scholarly_cache_dir, 0o700)
        os.chmod(zotero_cache_dir, 0o700)
        os.chmod(texmf_dir, 0o755)

        write_probe = config_dir / ".write_probe"
        write_probe.write_text("ok", encoding="utf-8")
        write_probe.unlink(missing_ok=True)
    except OSError as exc:
        raise ConfigPermissionError(
            f"Data volume '{resolved_dir}' is not writable ({exc}). "
            "Ensure the mounted host directory is owned by UID:GID 1000:1000 "
            "(e.g., `chown -R 1000:1000 ./data`)."
        ) from exc

    settings_file = config_dir / "settings.yaml"
    yaml_values: dict[str, Any] = {}
    if settings_file.is_file():
        try:
            loaded = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                yaml_values = loaded
        except yaml.YAMLError:
            yaml_values = {}

    merged: dict[str, Any] = dict(yaml_values)
    merged["data_dir"] = resolved_dir
    _apply_env_overrides(merged)

    settings = AppSettings(**merged)

    if not settings_file.exists():
        try:
            save_settings_yaml(settings)
        except OSError as exc:
            raise ConfigPermissionError(
                f"Failed to write default settings to '{settings_file}' ({exc}). "
                "Ensure the mounted host directory is owned by UID:GID 1000:1000."
            ) from exc

    return settings
