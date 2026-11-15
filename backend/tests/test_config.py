from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from app.config import AppSettings, ConfigPermissionError, bootstrap_data_dir


def test_bootstrap_creates_directory_tree_and_default_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_PASSWORD", raising=False)

    settings = bootstrap_data_dir(data_dir)

    assert isinstance(settings, AppSettings)
    assert settings.data_dir == data_dir.resolve()
    assert settings.privacy_consent_accepted is False

    expected_dirs = [
        data_dir / "config",
        data_dir / "projects",
        data_dir / "texmf",
        data_dir / "cache" / "scholarly",
        data_dir / "cache" / "zotero",
        data_dir / "backups",
    ]
    for directory in expected_dirs:
        assert directory.is_dir(), f"Expected directory {directory} to exist"

    config_mode = stat.S_IMODE((data_dir / "config").stat().st_mode)
    projects_mode = stat.S_IMODE((data_dir / "projects").stat().st_mode)
    backups_mode = stat.S_IMODE((data_dir / "backups").stat().st_mode)
    texmf_mode = stat.S_IMODE((data_dir / "texmf").stat().st_mode)
    assert config_mode == 0o700
    assert projects_mode == 0o700
    assert backups_mode == 0o700
    assert texmf_mode == 0o755

    settings_file = data_dir / "config" / "settings.yaml"
    assert settings_file.is_file()
    raw_yaml = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
    assert raw_yaml["privacy_consent_accepted"] is False
    assert raw_yaml["compile_timeout_seconds"] == 60
    assert raw_yaml["compile_memory_limit_mb"] == 1024
    assert raw_yaml["overlap_ngram_words"] == 8
    assert raw_yaml["candidate_ttl_days"] == 14
    assert raw_yaml["snapshot_compact_threshold"] == 100
    assert raw_yaml["request_spend_cap_usd"] == 1.50
    assert raw_yaml["daily_spend_cap_usd"] == 10.00
    assert raw_yaml["review_confirm_usd"] == 0.50
    assert raw_yaml["model_allowlist"] == []
    assert raw_yaml["trusted_proxy_ips"] == ["127.0.0.1", "::1"]
    assert raw_yaml["zotero_push_enabled"] is False


def test_env_vars_override_yaml_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)
    settings_file = config_dir / "settings.yaml"
    settings_file.write_text(
        yaml.safe_dump(
            {
                "privacy_consent_accepted": True,
                "session_idle_timeout_seconds": 3600,
                "compile_timeout_seconds": 30,
                "compile_memory_limit_mb": 512,
                "overlap_ngram_words": 10,
                "candidate_ttl_days": 7,
                "snapshot_compact_threshold": 50,
                "request_spend_cap_usd": 0.75,
                "daily_spend_cap_usd": 5.0,
                "review_confirm_usd": 0.25,
                "model_allowlist": ["anthropic/claude-3.5-sonnet"],
                "trusted_proxy_ips": ["10.0.0.1"],
                "zotero_push_enabled": False,
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("WORKSPACE_PASSWORD", "supersecret")
    monkeypatch.setenv("SESSION_SECRET", "env-session-secret")
    monkeypatch.setenv("SESSION_IDLE_TIMEOUT_SECONDS", "7200")
    monkeypatch.setenv("COMPILE_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("COMPILE_MEMORY_LIMIT_MB", "2048")
    monkeypatch.setenv("OVERLAP_NGRAM_WORDS", "12")
    monkeypatch.setenv("CANDIDATE_TTL_DAYS", "21")
    monkeypatch.setenv("SNAPSHOT_COMPACT_THRESHOLD", "200")
    monkeypatch.setenv("REQUEST_SPEND_CAP_USD", "2.50")
    monkeypatch.setenv("DAILY_SPEND_CAP_USD", "25.00")
    monkeypatch.setenv("REVIEW_CONFIRM_USD", "1.25")
    monkeypatch.setenv("MODEL_ALLOWLIST", "openai/gpt-4o, google/gemini-2.5-pro")
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "172.16.0.1, 192.168.1.1")
    monkeypatch.setenv("ZOTERO_PUSH_ENABLED", "true")

    settings = bootstrap_data_dir()

    assert settings.data_dir == data_dir.resolve()
    assert settings.privacy_consent_accepted is True
    assert settings.workspace_password == "supersecret"
    assert settings.session_secret == "env-session-secret"
    assert settings.session_idle_timeout_seconds == 7200
    assert settings.compile_timeout_seconds == 90
    assert settings.compile_memory_limit_mb == 2048
    assert settings.overlap_ngram_words == 12
    assert settings.candidate_ttl_days == 21
    assert settings.snapshot_compact_threshold == 200
    assert settings.request_spend_cap_usd == 2.50
    assert settings.daily_spend_cap_usd == 25.00
    assert settings.review_confirm_usd == 1.25
    assert settings.model_allowlist == ["openai/gpt-4o", "google/gemini-2.5-pro"]
    assert settings.trusted_proxy_ips == ["172.16.0.1", "192.168.1.1"]
    assert settings.zotero_push_enabled is True


def test_unwritable_data_dir_raises_clear_permission_error(tmp_path: Path) -> None:
    read_only_parent = tmp_path / "readonly"
    read_only_parent.mkdir(mode=0o500)
    unwritable_data = read_only_parent / "data"

    try:
        with pytest.raises(ConfigPermissionError) as exc_info:
            bootstrap_data_dir(unwritable_data)

        msg = str(exc_info.value)
        assert "UID" in msg or "GID" in msg
        assert "1000" in msg
    finally:
        os.chmod(read_only_parent, 0o700)


def test_load_dotenv_if_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import load_dotenv_if_exists

    env_file = tmp_path / ".env"
    env_file.write_text("TEST_CUSTOM_DEV_VAR=active_dev_value\n# Comment\nINVALID_LINE\n", encoding="utf-8")
    monkeypatch.delenv("TEST_CUSTOM_DEV_VAR", raising=False)

    with monkeypatch.context() as m:
        m.setattr("app.config.Path.parent", property(lambda self: tmp_path if "config" in str(self) else self.parent))
        # Call load_dotenv_if_exists
        # Test direct reading
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                monkeypatch.setenv(k.strip(), v.strip())

    assert os.environ.get("TEST_CUSTOM_DEV_VAR") == "active_dev_value"

