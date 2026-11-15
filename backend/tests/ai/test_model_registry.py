from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.ai.model_registry import EnabledModel, ModelRegistry, build_seed_models
from app.ai.openrouter import DEFAULT_FALLBACK_MODELS
from app.db import DatabaseManager


def _registry(tmp_path: Path) -> ModelRegistry:
    db = DatabaseManager(db_path=tmp_path / "app.db", backup_dir=tmp_path / "backups")
    db.initialize()
    return ModelRegistry(db)


def _model(model_id: str, **overrides: object) -> EnabledModel:
    values: dict[str, object] = {
        "id": model_id,
        "name": model_id.upper(),
        "context_length": 128000,
        "prompt_price_per_token": 0.000003,
        "completion_price_per_token": 0.000015,
        "supports_vision": False,
        "verified": True,
    }
    values.update(overrides)
    return EnabledModel(**values)  # type: ignore[arg-type]


def test_enable_and_list_preserves_order(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.enable(_model("b/model"))
    registry.enable(_model("a/model"))
    registry.enable(_model("c/model", supports_vision=True, verified=False))

    listed = registry.list_enabled()

    assert [m.id for m in listed] == ["b/model", "a/model", "c/model"]
    assert listed[2].supports_vision is True
    assert listed[2].verified is False
    assert listed[0].supports_vision is False
    assert listed[0].verified is True


def test_enable_is_idempotent_and_keeps_position(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.enable(_model("a/model"))
    registry.enable(_model("b/model"))

    refreshed = registry.enable(_model("a/model", name="Renamed", context_length=1))

    listed = registry.list_enabled()
    assert [m.id for m in listed] == ["a/model", "b/model"]
    assert listed[0].name == "Renamed"
    assert listed[0].context_length == 1
    assert refreshed.name == "Renamed"


def test_disable_removes_and_returns_true(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.enable(_model("a/model"))
    registry.enable(_model("b/model"))

    assert registry.disable("a/model") is True

    assert [m.id for m in registry.list_enabled()] == ["b/model"]


def test_disable_unknown_returns_false(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert registry.disable("missing/model") is False


def test_is_enabled(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.enable(_model("a/model"))

    assert registry.is_enabled("a/model") is True
    assert registry.is_enabled("b/model") is False


def test_seed_runs_once(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert registry.seed_if_needed([_model("a/model"), _model("b/model")]) is True
    assert registry.seed_if_needed([_model("c/model")]) is False

    assert [m.id for m in registry.list_enabled()] == ["a/model", "b/model"]


def test_seed_does_not_repeat_after_list_emptied(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.seed_if_needed([_model("a/model")])
    registry.disable("a/model")

    assert registry.seed_if_needed([_model("a/model")]) is False

    assert registry.list_enabled() == []


def test_seed_is_atomic_and_stays_unseeded_on_failure(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    with pytest.raises(sqlite3.Error):
        registry.seed_if_needed([_model("a/model"), _model("b/model", name=object())])

    assert registry.list_enabled() == []
    assert registry.seed_if_needed([_model("c/model")]) is True
    assert [m.id for m in registry.list_enabled()] == ["c/model"]


def test_build_seed_from_allowlist_uses_ids() -> None:
    seeded = build_seed_models(
        ["openai/gpt-4o", "custom/unknown-model"], DEFAULT_FALLBACK_MODELS
    )

    assert [m.id for m in seeded] == ["openai/gpt-4o", "custom/unknown-model"]
    known, unknown = seeded
    assert known.verified is True
    assert known.name == "GPT-4o"
    assert known.supports_vision is True
    assert unknown.verified is False
    assert unknown.name == "custom/unknown-model"
    assert unknown.prompt_price_per_token == 0.000003
    assert unknown.completion_price_per_token == 0.000015
    assert unknown.context_length == 128000
    assert unknown.supports_vision is False


def test_build_seed_without_allowlist_uses_fallback_models() -> None:
    seeded = build_seed_models([], DEFAULT_FALLBACK_MODELS)

    assert [m.id for m in seeded] == [m.id for m in DEFAULT_FALLBACK_MODELS]
    assert all(m.verified for m in seeded)
