from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from app.config_secrets import ALLOWED_SECRET_KEYS, SecretsStore


def test_secrets_saved_with_0600_mode_lowercase_keys_and_auto_session_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env_key in (
        "OPENROUTER_API_KEY",
        "ZOTERO_API_KEY",
        "ZOTERO_LIBRARY_ID",
        "ZOTERO_LIBRARY_TYPE",
        "UNPAYWALL_EMAIL",
        "GITHUB_TOKEN",
        "SESSION_SECRET",
    ):
        monkeypatch.delenv(env_key, raising=False)

    secrets_path = tmp_path / "config" / "secrets.yaml"
    store = SecretsStore(secrets_path=secrets_path)

    assert secrets_path.is_file()
    mode = stat.S_IMODE(secrets_path.stat().st_mode)
    assert mode == 0o600

    session_sec = store.get("session_secret")
    assert session_sec is not None
    assert len(session_sec) == 64  # 32-byte hex string

    # Update secrets with mixed-case or lowercase keys
    store.update_secrets(
        {
            "openrouter_api_key": "sk-or-v1-secret-abc",
            "zotero_api_key": "zot-key-xyz",
            "zotero_library_id": "123456",
            "zotero_library_type": "user",
            "unpaywall_email": "researcher@university.edu",
        }
    )

    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    raw_yaml = yaml.safe_load(secrets_path.read_text(encoding="utf-8"))
    assert set(raw_yaml.keys()).issubset(ALLOWED_SECRET_KEYS)
    for key in raw_yaml:
        assert key == key.lower()

    assert store.get("openrouter_api_key") == "sk-or-v1-secret-abc"
    assert store.get("zotero_api_key") == "zot-key-xyz"

    # Environment variable override takes precedence over secrets.yaml
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env-override")
    assert store.get("openrouter_api_key") == "sk-or-env-override"

    masked = store.get_masked_status()
    assert masked == {
        "has_openrouter_key": True,
        "has_zotero_key": True,
        "has_zotero_library_id": True,
        "has_unpaywall_email": True,
        "has_github_token": False,
        "has_session_secret": True,
    }
    for val in masked.values():
        assert isinstance(val, bool)
        assert "sk-or" not in str(val)


def test_named_github_and_zotero_keys_stored_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env_key in ("GITHUB_TOKEN", "ZOTERO_API_KEY"):
        monkeypatch.delenv(env_key, raising=False)

    secrets_path = tmp_path / "config" / "secrets.yaml"
    store = SecretsStore(secrets_path=secrets_path)

    store.set_named_key("github", "personal", "ghp_personal_token_123")
    store.set_named_key("github", "work", "ghp_work_token_456")
    store.set_named_key("zotero", "lab-zot", "zot_lab_key_789")

    assert store.list_github_tokens() == [{"name": "personal"}, {"name": "work"}]
    assert store.list_zotero_keys() == [{"name": "lab-zot"}]

    # Reload from disk and verify persistence
    reloaded = SecretsStore(secrets_path=secrets_path)
    assert reloaded.list_github_tokens() == [{"name": "personal"}, {"name": "work"}]
    assert reloaded.list_zotero_keys() == [{"name": "lab-zot"}]

    tok, name, src = reloaded.get_github_token_for_project(token_name="work")
    assert (tok, name, src) == ("ghp_work_token_456", "work", "saved")

    reloaded.set_project_github_token_name("proj-1", "work")
    tok_proj, name_proj, _ = reloaded.get_github_token_for_project(project_id="proj-1")
    assert (tok_proj, name_proj) == ("ghp_work_token_456", "work")

    zot_key, zot_name = reloaded.get_zotero_key_for_project(key_name="lab-zot")
    assert (zot_key, zot_name) == ("zot_lab_key_789", "lab-zot")

    redactable = reloaded.get_redactable_values()
    assert "ghp_personal_token_123" in redactable
    assert "ghp_work_token_456" in redactable
    assert "zot_lab_key_789" in redactable

    assert reloaded.remove_named_key("github", "work") is True
    assert reloaded.list_github_tokens() == [{"name": "personal"}]

