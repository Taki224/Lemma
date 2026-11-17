from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai.model_registry import EnabledModel
from app.ai.openrouter import OpenRouterClient
from app.config import bootstrap_data_dir
from app.main import create_app


def _make_app(tmp_path: Path):
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "models-router-pass",
            "privacy_consent_accepted": True,
        }
    )
    return create_app(settings=settings)


def _login(client: TestClient) -> dict[str, str]:
    res = client.post("/api/auth/login", json={"password": "models-router-pass"})
    assert res.status_code == 200
    return {"X-CSRF-Token": res.json()["csrf_token"]}


def _model(model_id: str, **overrides: object) -> EnabledModel:
    values: dict[str, object] = {
        "id": model_id,
        "name": model_id.upper(),
        "context_length": 1000,
        "prompt_price_per_token": 0.000001,
        "completion_price_per_token": 0.000002,
        "supports_vision": False,
        "verified": True,
    }
    values.update(overrides)
    return EnabledModel(**values)  # type: ignore[arg-type]


def test_list_enabled_returns_ordered_models(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        registry = app.state.model_registry
        for existing in registry.list_enabled():
            registry.disable(existing.id)
        registry.enable(_model("b/model", supports_vision=True))
        registry.enable(_model("a/model", verified=False))

        res = client.get("/api/ai/models")

    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"models"}
    assert [m["id"] for m in body["models"]] == ["b/model", "a/model"]
    assert body["models"][0] == {
        "id": "b/model",
        "name": "B/MODEL",
        "context_length": 1000,
        "prompt_price_per_token": 0.000001,
        "completion_price_per_token": 0.000002,
        "supports_vision": True,
        "verified": True,
        "provider": "openrouter",
        "base_url": "",
    }
    assert body["models"][1]["verified"] is False


def test_list_requires_auth(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        res = client.get("/api/ai/models")

    assert res.status_code == 401


_CATALOG = {
    "data": [
        {
            "id": f"vendor/model-{i:03d}",
            "name": f"Model {i:03d}",
            "context_length": 4096,
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        }
        for i in range(150)
    ]
    + [
        {
            "id": "mistralai/mistral-large",
            "name": "Mistral Large",
            "context_length": 32000,
            "pricing": {"prompt": "0.000002", "completion": "0.000006"},
        }
    ]
}


def _install_catalog_client(app, status_code: int = 200) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if status_code != 200:
            return httpx.Response(status_code, json={})
        return httpx.Response(200, json=_CATALOG)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    app.state.openrouter_client = OpenRouterClient(
        db=app.state.db,
        secrets=app.state.secrets_store,
        settings=app.state.settings,
        registry=app.state.model_registry,
        http_client=http_client,
    )


def test_catalog_search_returns_matches(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        _install_catalog_client(app)

        res = client.get("/api/settings/models/catalog", params={"q": "MISTRAL"})

    assert res.status_code == 200
    body = res.json()
    assert body["catalog_available"] is True
    assert [m["id"] for m in body["models"]] == ["mistralai/mistral-large"]
    assert body["models"][0]["name"] == "Mistral Large"
    assert body["models"][0]["context_length"] == 32000


def test_catalog_limit_is_clamped(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        _install_catalog_client(app)

        default = client.get("/api/settings/models/catalog", params={"q": ""})
        huge = client.get("/api/settings/models/catalog", params={"q": "", "limit": 5000})
        zero = client.get("/api/settings/models/catalog", params={"q": "", "limit": 0})
        negative = client.get("/api/settings/models/catalog", params={"q": "", "limit": -4})

    assert len(default.json()["models"]) == 25
    assert len(huge.json()["models"]) == 100
    assert len(zero.json()["models"]) == 1
    assert len(negative.json()["models"]) == 1


def test_catalog_reports_unavailable_when_fallback_used(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        _install_catalog_client(app, status_code=500)

        res = client.get("/api/settings/models/catalog", params={"q": "gpt"})

    assert res.status_code == 200
    body = res.json()
    assert body["catalog_available"] is False
    assert [m["id"] for m in body["models"]] == ["openai/gpt-4o"]


def test_catalog_requires_auth(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        res = client.get("/api/settings/models/catalog", params={"q": "x"})

    assert res.status_code == 401


def _enabled_ids(client: TestClient) -> list[str]:
    return [m["id"] for m in client.get("/api/ai/models").json()["models"]]


def test_enable_catalog_model_hydrates_metadata(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        _install_catalog_client(app)

        res = client.post(
            "/api/settings/models",
            json={"model_id": "mistralai/mistral-large", "supports_vision": True},
            headers=headers,
        )

    assert res.status_code == 201
    body = res.json()
    assert body["id"] == "mistralai/mistral-large"
    assert body["name"] == "Mistral Large"
    assert body["context_length"] == 32000
    assert body["prompt_price_per_token"] == 0.000002
    assert body["completion_price_per_token"] == 0.000006
    assert body["verified"] is True
    # Catalog metadata wins: the supplied vision flag is ignored.
    assert body["supports_vision"] is False


def test_enable_unknown_id_is_unverified_with_default_pricing(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        _install_catalog_client(app)

        res = client.post(
            "/api/settings/models",
            json={"model_id": "custom/brand-new"},
            headers=headers,
        )
        pricing = app.state.openrouter_client._pricing_map["custom/brand-new"]

    assert res.status_code == 201
    body = res.json()
    assert body["id"] == "custom/brand-new"
    assert body["name"] == "custom/brand-new"
    assert body["verified"] is False
    assert body["supports_vision"] is False
    assert body["context_length"] == 128000
    assert body["prompt_price_per_token"] == 0.000003
    assert body["completion_price_per_token"] == 0.000015
    assert pricing == (0.000003, 0.000015)


def test_enable_unknown_id_honors_vision_flag(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        _install_catalog_client(app)

        res = client.post(
            "/api/settings/models",
            json={"model_id": "custom/seer", "supports_vision": True},
            headers=headers,
        )

    assert res.status_code == 201
    assert res.json()["supports_vision"] is True
    assert res.json()["verified"] is False


def test_enable_rejects_empty_id(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        _install_catalog_client(app)

        empty = client.post("/api/settings/models", json={"model_id": ""}, headers=headers)
        blank = client.post("/api/settings/models", json={"model_id": "   "}, headers=headers)
        too_long = client.post(
            "/api/settings/models", json={"model_id": "x" * 201}, headers=headers
        )

    assert empty.status_code == 422
    assert blank.status_code == 422
    assert too_long.status_code == 422


def test_enable_is_idempotent(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        _install_catalog_client(app)

        first = client.post(
            "/api/settings/models", json={"model_id": "custom/once"}, headers=headers
        )
        second = client.post(
            "/api/settings/models", json={"model_id": "custom/once"}, headers=headers
        )
        ids = _enabled_ids(client)

    assert first.status_code == 201
    assert second.status_code == 201
    assert ids.count("custom/once") == 1
    assert ids[-1] == "custom/once"


def test_disable_with_slash_in_id(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        _install_catalog_client(app)
        client.post(
            "/api/settings/models",
            json={"model_id": "mistralai/mistral-large"},
            headers=headers,
        )
        assert "mistralai/mistral-large" in _enabled_ids(client)

        res = client.delete("/api/settings/models/mistralai/mistral-large", headers=headers)

        assert res.status_code == 204
        assert res.content == b""
        assert "mistralai/mistral-large" not in _enabled_ids(client)


def test_disable_unknown_returns_404(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)

        res = client.delete("/api/settings/models/not/enabled", headers=headers)

    assert res.status_code == 404
    assert res.json()["detail"] == "Model is not enabled."


def test_writes_require_csrf(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        _install_catalog_client(app)
        before = _enabled_ids(client)

        post_res = client.post("/api/settings/models", json={"model_id": "custom/nope"})
        delete_res = client.delete(f"/api/settings/models/{before[0]}")
        after = _enabled_ids(client)

    assert post_res.status_code == 403
    assert delete_res.status_code == 403
    assert after == before


def test_writes_require_auth(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        post_res = client.post("/api/settings/models", json={"model_id": "custom/nope"})
        delete_res = client.delete("/api/settings/models/custom/nope")

    assert post_res.status_code == 401
    assert delete_res.status_code == 401


def test_enable_then_list_then_disable_roundtrip(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        _install_catalog_client(app)
        baseline = _enabled_ids(client)

        enable_res = client.post(
            "/api/settings/models",
            json={"model_id": "mistralai/mistral-large"},
            headers=headers,
        )
        after_enable = _enabled_ids(client)
        disable_res = client.delete(
            "/api/settings/models/mistralai/mistral-large", headers=headers
        )
        after_disable = _enabled_ids(client)

    assert enable_res.status_code == 201
    assert after_enable == baseline + ["mistralai/mistral-large"]
    assert disable_res.status_code == 204
    assert after_disable == baseline


def test_startup_seeds_registry_once(tmp_path: Path) -> None:
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "models-router-pass",
            "privacy_consent_accepted": True,
            "model_allowlist": ["openai/gpt-4o"],
        }
    )

    first_app = create_app(settings=settings)
    with TestClient(first_app) as client:
        headers = _login(client)
        assert _enabled_ids(client) == ["openai/gpt-4o"]
        delete_res = client.delete("/api/settings/models/openai/gpt-4o", headers=headers)
        assert delete_res.status_code == 204
        assert _enabled_ids(client) == []

    # Restart on the same data dir: emptying the list must never trigger a re-seed.
    second_app = create_app(settings=settings)
    with TestClient(second_app) as client:
        _login(client)
        assert _enabled_ids(client) == []


def test_startup_without_allowlist_seeds_fallback_models(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client)

        assert _enabled_ids(client) == [
            "anthropic/claude-3.7-sonnet",
            "openai/gpt-4o",
            "google/gemini-2.5-pro",
        ]


def test_disabled_model_is_rejected_by_chat_endpoint(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        project_id = client.post(
            "/api/projects", json={"name": "Chat 403"}, headers=headers
        ).json()["project_id"]
        chat_id = client.post(
            f"/api/projects/{project_id}/chats",
            json={"title": "Chat"},
            headers=headers,
        ).json()["chat_id"]

        assert (
            client.delete(
                "/api/settings/models/anthropic/claude-3.7-sonnet", headers=headers
            ).status_code
            == 204
        )

        res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "Polish introduction.",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "writing",
            },
            headers=headers,
        )

    assert res.status_code == 403
    assert "not enabled" in res.json()["detail"]


def test_enable_fallback_does_not_downgrade_verified_model(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        _install_catalog_client(app)
        first = client.post(
            "/api/settings/models",
            json={"model_id": "mistralai/mistral-large"},
            headers=headers,
        )
        assert first.json()["verified"] is True

        _install_catalog_client(app, status_code=500)
        with patch.object(
            app.state.openrouter_client, "set_model_pricing"
        ) as mock_pricing:
            second = client.post(
                "/api/settings/models",
                json={"model_id": "mistralai/mistral-large"},
                headers=headers,
            )
            mock_pricing.assert_not_called()
        listed = client.get("/api/ai/models").json()["models"]

    assert second.status_code == 201
    assert second.json() == first.json()
    row = next(m for m in listed if m["id"] == "mistralai/mistral-large")
    assert row["verified"] is True
    assert row["name"] == "Mistral Large"
    assert row["context_length"] == 32000
    assert row["prompt_price_per_token"] == pytest.approx(0.000002)
    assert row["completion_price_per_token"] == pytest.approx(0.000006)


def test_startup_hydrates_pricing_for_enabled_models(tmp_path: Path) -> None:
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "models-router-pass",
            "privacy_consent_accepted": True,
        }
    )
    first_app = create_app(settings=settings)
    with TestClient(first_app):
        first_app.state.model_registry.enable(
            _model(
                "custom/pricey",
                prompt_price_per_token=0.00001,
                completion_price_per_token=0.00004,
            )
        )

    second_app = create_app(settings=settings)
    with TestClient(second_app):
        cost = second_app.state.openrouter_client.estimate_cost_usd(
            "custom/pricey", 1000, 1000
        )

    assert cost == pytest.approx(1000 * 0.00001 + 1000 * 0.00004)


def test_catalog_query_longer_than_200_is_rejected(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        _login(client)
        _install_catalog_client(app)

        ok = client.get("/api/settings/models/catalog", params={"q": "x" * 200})
        too_long = client.get("/api/settings/models/catalog", params={"q": "x" * 201})

    assert ok.status_code == 200
    assert too_long.status_code == 422


def test_disable_with_url_encoded_slash(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        _install_catalog_client(app)
        client.post(
            "/api/settings/models",
            json={"model_id": "mistralai/mistral-large"},
            headers=headers,
        )
        assert "mistralai/mistral-large" in _enabled_ids(client)

        res = client.delete("/api/settings/models/mistralai%2Fmistral-large", headers=headers)

        assert res.status_code == 204
        assert "mistralai/mistral-large" not in _enabled_ids(client)


def test_enable_body_validation(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        _install_catalog_client(app)

        missing = client.post("/api/settings/models", json={}, headers=headers)
        bad_vision = client.post(
            "/api/settings/models",
            json={"model_id": "custom/v", "supports_vision": "maybe"},
            headers=headers,
        )
        long_id = "x" * 200
        max_len = client.post(
            "/api/settings/models", json={"model_id": long_id}, headers=headers
        )
        trimmed = client.post(
            "/api/settings/models", json={"model_id": " custom/trim "}, headers=headers
        )
        ids = _enabled_ids(client)

    assert missing.status_code == 422
    assert bad_vision.status_code == 422
    assert max_len.status_code == 201
    assert max_len.json()["id"] == long_id
    assert max_len.json()["prompt_price_per_token"] == pytest.approx(0.000003)
    assert trimmed.status_code == 201
    assert trimmed.json()["id"] == "custom/trim"
    assert "custom/trim" in ids


def test_local_model_config_catalog_and_enable_endpoints(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    def local_handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://localhost:1234/v1/models":
            return httpx.Response(
                200,
                json={"data": [{"id": "qwen2.5-coder:7b", "context_length": 32768}]},
            )
        return httpx.Response(404)

    with TestClient(app) as client:
        headers = _login(client)
        app.state.openrouter_client._http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(local_handler)
        )

        cfg_get = client.get("/api/settings/models/local/config")
        assert cfg_get.status_code == 200
        assert cfg_get.json()["base_url"] == "http://localhost:11434/v1"

        bad_cfg = client.put(
            "/api/settings/models/local/config",
            json={"base_url": "ftp://localhost:1234/v1"},
            headers=headers,
        )
        assert bad_cfg.status_code == 400

        cfg_put = client.put(
            "/api/settings/models/local/config",
            json={"base_url": "http://localhost:1234/v1/"},
            headers=headers,
        )
        assert cfg_put.status_code == 200
        assert cfg_put.json()["base_url"] == "http://localhost:1234/v1"

        catalog = client.get("/api/settings/models/local/catalog")
        assert catalog.status_code == 200
        assert catalog.json()["reachable"] is True
        assert catalog.json()["base_url"] == "http://localhost:1234/v1"
        assert [m["id"] for m in catalog.json()["models"]] == ["qwen2.5-coder:7b"]

        enable_verified = client.post(
            "/api/settings/models",
            json={
                "model_id": "qwen2.5-coder:7b",
                "provider": "local",
                "base_url": "http://localhost:1234/v1",
            },
            headers=headers,
        )
        assert enable_verified.status_code == 201
        v_body = enable_verified.json()
        assert v_body["provider"] == "local"
        assert v_body["base_url"] == "http://localhost:1234/v1"
        assert v_body["verified"] is True
        assert v_body["prompt_price_per_token"] == 0.0
        assert v_body["completion_price_per_token"] == 0.0

        enable_manual = client.post(
            "/api/settings/models",
            json={
                "model_id": "my-custom-gguf",
                "provider": "local",
                "supports_vision": True,
            },
            headers=headers,
        )
        assert enable_manual.status_code == 201
        m_body = enable_manual.json()
        assert m_body["provider"] == "local"
        assert m_body["base_url"] == "http://localhost:1234/v1"
        assert m_body["verified"] is False
        assert m_body["supports_vision"] is True


def test_chat_naming_get_put_and_validation(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        _install_catalog_client(app)

        initial = client.get("/api/settings/chat-naming")
        assert initial.status_code == 200
        assert initial.json() == {"enabled": False, "model": ""}

        no_model = client.put(
            "/api/settings/chat-naming",
            json={"enabled": True, "model": ""},
            headers=headers,
        )
        assert no_model.status_code == 400

        not_enabled = client.put(
            "/api/settings/chat-naming",
            json={"enabled": True, "model": "not/enabled"},
            headers=headers,
        )
        assert not_enabled.status_code == 400

        ok = client.put(
            "/api/settings/chat-naming",
            json={"enabled": True, "model": "openai/gpt-4o"},
            headers=headers,
        )
        assert ok.status_code == 200
        assert ok.json() == {"enabled": True, "model": "openai/gpt-4o"}

        reloaded = client.get("/api/settings/chat-naming")
        assert reloaded.json() == {"enabled": True, "model": "openai/gpt-4o"}


def test_web_search_setting_round_trip(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    with TestClient(app) as client:
        headers = _login(client)
        assert client.get("/api/settings/web-search").json() == {"enabled": False}
        ok = client.put("/api/settings/web-search", json={"enabled": True}, headers=headers)
        assert ok.status_code == 200
        assert ok.json() == {"enabled": True}
        assert client.get("/api/settings/web-search").json() == {"enabled": True}
