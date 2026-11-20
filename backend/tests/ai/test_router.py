from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ai.openrouter import OpenRouterClient
from app.config import bootstrap_data_dir
from app.main import create_app


def _login_and_get_csrf(client: TestClient, password: str = "ai-router-pass") -> dict[str, str]:
    res = client.post("/api/auth/login", json={"password": password})
    assert res.status_code == 200
    csrf = res.json()["csrf_token"]
    return {"X-CSRF-Token": csrf}


def test_prompts_get_and_put_with_invalid_yaml_warning(tmp_path: Path) -> None:
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "ai-router-pass",
            "privacy_consent_accepted": True,
        }
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        # Unauthenticated request rejected
        unauth = client.get("/api/ai/prompts")
        assert unauth.status_code == 401

        headers = _login_and_get_csrf(client)

        # GET /api/ai/prompts returns seeded defaults with no syntax_warning
        get_res = client.get("/api/ai/prompts")
        assert get_res.status_code == 200
        data = get_res.json()
        assert data["syntax_warning"] is None
        assert data["error_line"] is None
        assert "writing" in data["profiles"]
        assert "compile_autofix" in data["profiles"]
        assert "raw_yaml" in data

        # PUT /api/ai/prompts with invalid YAML syntax returns syntax_warning + line number
        # while retaining last valid profiles
        bad_yaml = (
            "profiles:\n"
            "  writing:\n"
            '    name: "Academic Writer"\n'
            "    role: [broken_yaml_bracket\n"
        )
        put_res = client.put(
            "/api/ai/prompts",
            json={"raw_yaml": bad_yaml},
            headers=headers,
        )
        assert put_res.status_code == 200
        put_data = put_res.json()
        assert put_data["syntax_warning"] is not None
        assert "line" in put_data["syntax_warning"].lower()
        assert put_data["error_line"] is not None
        assert put_data["error_line"] >= 4
        # Profiles still retained from last valid state
        assert "writing" in put_data["profiles"]


def test_compile_autofix_endpoint_returns_proposed_hunk(tmp_path: Path) -> None:
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "ai-router-pass",
            "privacy_consent_accepted": True,
        }
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)

        # Inject a mocked OpenRouterClient onto app.state so no real network call is made
        def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    "Here is the fix:\n"
                                    "```latex\n"
                                    "\\section{Introduction}\n"
                                    "Fixed line with \\textbf{valid command} \\cite{vaswani2017attention}.\n"
                                    "```"
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 90, "completion_tokens": 45},
                },
            )

        transport = httpx.MockTransport(mock_handler)
        http_client = httpx.AsyncClient(
            transport=transport, base_url="https://openrouter.ai/api/v1"
        )
        app.state.secrets_store.update_secrets(
            {"openrouter_api_key": "sk-or-v1-router-test-key"}
        )
        app.state.openrouter_client = OpenRouterClient(
            db=app.state.db,
            secrets=app.state.secrets_store,
            settings=app.state.settings,
            registry=app.state.model_registry,
            http_client=http_client,
        )
        app.state.chat_service.openrouter = app.state.openrouter_client

        # Create a project
        proj_res = client.post(
            "/api/projects",
            json={"name": "Autofix Study"},
            headers=headers,
        )
        assert proj_res.status_code == 201
        project_id = proj_res.json()["project_id"]

        # GET /api/ai/models
        models_res = client.get("/api/ai/models")
        assert models_res.status_code == 200
        models_body = models_res.json()
        assert set(models_body) == {"models"}
        assert "anthropic/claude-3.7-sonnet" in [m["id"] for m in models_body["models"]]

        # POST /api/projects/{project_id}/compile/autofix
        autofix_res = client.post(
            f"/api/projects/{project_id}/compile/autofix",
            json={
                "diagnostic": {
                    "severity": "error",
                    "file_path": "main.tex",
                    "line": 10,
                    "message": "Undefined control sequence \\badcmd.",
                    "context_lines": "l.10 \\badcmd{test}",
                    "missing_package": None,
                },
                "model": "anthropic/claude-3.7-sonnet",
            },
            headers=headers,
        )
        assert autofix_res.status_code == 200
        hunk = autofix_res.json()["hunks"][0]
        assert hunk["file_path"] == "main.tex"
        assert hunk["base_etag"]
        assert "\\textbf{valid command}" in hunk["replacement_text"]
        assert "vaswani2017attention" in hunk["unknown_cite_keys"]

        # Also test chat creation and message send endpoints
        chat_res = client.post(
            f"/api/projects/{project_id}/chats",
            json={"title": "Introduction Review"},
            headers=headers,
        )
        assert chat_res.status_code == 201
        chat_id = chat_res.json()["chat_id"]

        list_chats_res = client.get(f"/api/projects/{project_id}/chats")
        assert list_chats_res.status_code == 200
        assert any(c["chat_id"] == chat_id for c in list_chats_res.json()["chats"])

        msg_res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "Polish introduction.",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "writing",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert msg_res.status_code == 200
        msg_data = msg_res.json()
        assert msg_data["chat_id"] == chat_id
        assert len(msg_data["hunks"]) == 1

        # Test list_messages
        msgs_list_res = client.get(f"/api/projects/{project_id}/chats/{chat_id}/messages")
        assert msgs_list_res.status_code == 200
        assert len(msgs_list_res.json()["messages"]) >= 2

        # Negative tests: 404 on nonexistent project
        nonexistent_res = client.get("/api/projects/nonexistent-proj-999/chats")
        assert nonexistent_res.status_code == 404

        # Negative test: 403 on unauthenticated request
        unauth_msg = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={"prompt": "Unauthenticated attempt"},
        )
        assert unauth_msg.status_code in {401, 403}


def _setup_chat_app(tmp_path: Path, reply: str):
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "ai-router-pass",
            "privacy_consent_accepted": True,
        }
    )
    app = create_app(settings=settings)
    captured: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": reply}}],
                "usage": {"prompt_tokens": 90, "completion_tokens": 45},
            },
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_handler),
        base_url="https://openrouter.ai/api/v1",
    )
    return app, http_client, captured


def _install_mock_openrouter(app, http_client) -> None:
    app.state.secrets_store.update_secrets({"openrouter_api_key": "sk-or-v1-router-test-key"})
    app.state.openrouter_client = OpenRouterClient(
        db=app.state.db,
        secrets=app.state.secrets_store,
        settings=app.state.settings,
        registry=app.state.model_registry,
        http_client=http_client,
    )
    app.state.chat_service.openrouter = app.state.openrouter_client


def test_send_chat_message_with_compile_autofix_profile_returns_400(tmp_path: Path) -> None:
    app, http_client, captured = _setup_chat_app(tmp_path, "unused")
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        project_id = client.post(
            "/api/projects", json={"name": "Reject Profile"}, headers=headers
        ).json()["project_id"]
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "T"}, headers=headers
        ).json()["chat_id"]

        res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "Fix it.",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "compile_autofix",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert res.status_code == 400
        assert "compile_autofix" in res.json()["detail"]
        assert captured == []


def test_auto_profile_routes_a_proofreading_prompt(tmp_path: Path) -> None:
    app, http_client, captured = _setup_chat_app(tmp_path, "Noted.")
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        project_id = client.post(
            "/api/projects", json={"name": "Routed"}, headers=headers
        ).json()["project_id"]
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "T"}, headers=headers
        ).json()["chat_id"]

        res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "Proofread this paragraph.",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "auto",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert res.status_code == 200, res.text
        assert res.json()["profile_id"] == "proofreading"
        body = json.loads(captured[-1].content)
        assert "academic proofreader" in body["messages"][0]["content"].lower()
        assert len(captured) == 1


def test_smart_routing_model_chooses_the_profile(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        system = ""
        messages = body.get("messages") or []
        if messages and isinstance(messages[0], dict):
            system = str(messages[0].get("content") or "")
        content = "synthesis" if "exactly one of" in system else "Drafted."
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4},
            },
        )

    app, _http_client, captured = _setup_chat_app(tmp_path, "unused")
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        app.state.settings.smart_routing_enabled = True
        app.state.settings.smart_routing_model = "anthropic/claude-3.7-sonnet"
        project_id = client.post(
            "/api/projects", json={"name": "Smart"}, headers=headers
        ).json()["project_id"]
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "T"}, headers=headers
        ).json()["chat_id"]

        res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "Draft the introduction.",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "auto",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert res.status_code == 200, res.text
        assert res.json()["profile_id"] == "synthesis"
        assert len(captured) == 2
        first = json.loads(captured[0].content)
        assert first["model"] == "anthropic/claude-3.7-sonnet"
        assert first["max_tokens"] == 16
        assert "exactly one of" in first["messages"][0]["content"]
        second = json.loads(captured[1].content)
        assert second["model"] == "anthropic/claude-3.7-sonnet"
        assert "Draft the introduction." in json.dumps(second["messages"])
        assert "exactly one of" not in second["messages"][0]["content"]


def test_smart_routing_passes_recent_history_to_classifier(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        messages = body.get("messages") or []
        system = str(messages[0].get("content") or "") if messages else ""
        content = "explain" if "exactly one of" in system else "Here are the model pairs."
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4},
            },
        )

    app, _http_client, captured = _setup_chat_app(tmp_path, "unused")
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        app.state.settings.smart_routing_enabled = True
        app.state.settings.smart_routing_model = "anthropic/claude-3.7-sonnet"
        project_id = client.post(
            "/api/projects", json={"name": "SmartHistory"}, headers=headers
        ).json()["project_id"]
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "T"}, headers=headers
        ).json()["chat_id"]

        first_res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "which models should i use for the tests",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "auto",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert first_res.status_code == 200, first_res.text
        assert first_res.json()["profile_id"] == "explain"

        second_res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "you didnt finish?",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "auto",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert second_res.status_code == 200, second_res.text
        assert second_res.json()["profile_id"] == "explain"

        # Third HTTP call is the router for the second user message.
        second_router_body = json.loads(captured[2].content)
        router_user_msg = second_router_body["messages"][1]["content"]
        assert "Recent conversation (for context only):" in router_user_msg
        assert "which models should i use for the tests" in router_user_msg
        assert "Here are the model pairs." in router_user_msg
        assert router_user_msg.endswith("Request to classify:\nyou didnt finish?")


def test_explicit_profile_skips_smart_routing(tmp_path: Path) -> None:
    app, http_client, captured = _setup_chat_app(tmp_path, "Drafted.")
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        app.state.settings.smart_routing_enabled = True
        app.state.settings.smart_routing_model = "anthropic/claude-3.7-sonnet"
        project_id = client.post(
            "/api/projects", json={"name": "Explicit"}, headers=headers
        ).json()["project_id"]
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "T"}, headers=headers
        ).json()["chat_id"]

        res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "Proofread this paragraph.",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "writing",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert res.status_code == 200, res.text
        assert res.json()["profile_id"] == "writing"
        assert len(captured) == 1


def test_smart_routing_falls_back_when_the_model_call_fails(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        system = ""
        messages = body.get("messages") or []
        if messages and isinstance(messages[0], dict):
            system = str(messages[0].get("content") or "")
        if "exactly one of" in system:
            return httpx.Response(500, json={"error": "router down"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Checked."}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4},
            },
        )

    app, _http_client, captured = _setup_chat_app(tmp_path, "unused")
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        app.state.settings.smart_routing_enabled = True
        app.state.settings.smart_routing_model = "anthropic/claude-3.7-sonnet"
        project_id = client.post(
            "/api/projects", json={"name": "Fallback"}, headers=headers
        ).json()["project_id"]
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "T"}, headers=headers
        ).json()["chat_id"]

        res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "Proofread this paragraph.",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "auto",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert res.status_code == 200, res.text
        assert res.json()["profile_id"] == "proofreading"
        assert len(captured) == 2


def test_smart_routing_falls_back_when_the_classifier_cannot_connect(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = ""
        messages = body.get("messages") or []
        if messages and isinstance(messages[0], dict):
            system = str(messages[0].get("content") or "")
        if "exactly one of" in system:
            raise httpx.ConnectError("classifier unreachable")
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Checked."}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 4},
            },
        )

    app, _http_client, captured = _setup_chat_app(tmp_path, "unused")
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        app.state.settings.smart_routing_enabled = True
        app.state.settings.smart_routing_model = "anthropic/claude-3.7-sonnet"
        project_id = client.post(
            "/api/projects", json={"name": "Connect"}, headers=headers
        ).json()["project_id"]
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "T"}, headers=headers
        ).json()["chat_id"]

        res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "Proofread this paragraph.",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "auto",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert res.status_code == 200, res.text
        assert res.json()["profile_id"] == "proofreading"
        assert len(captured) == 1


def test_disabled_reply_model_does_not_call_the_router(tmp_path: Path) -> None:
    app, http_client, captured = _setup_chat_app(tmp_path, "Drafted.")
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        app.state.settings.smart_routing_enabled = True
        app.state.settings.smart_routing_model = "anthropic/claude-3.7-sonnet"
        project_id = client.post(
            "/api/projects", json={"name": "Disabled"}, headers=headers
        ).json()["project_id"]
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "T"}, headers=headers
        ).json()["chat_id"]

        res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "Proofread this paragraph.",
                "model": "not/enabled",
                "profile_id": "auto",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert res.status_code == 403
        assert captured == []


def test_smart_routing_settings_round_trip(tmp_path: Path) -> None:
    app, http_client, _captured = _setup_chat_app(tmp_path, "unused")
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        initial = client.get("/api/settings/smart-routing")
        assert initial.status_code == 200
        assert initial.json() == {"enabled": False, "model": ""}

        blank = client.put(
            "/api/settings/smart-routing",
            json={"enabled": True, "model": "  "},
            headers=headers,
        )
        assert blank.status_code == 400

        denied = client.put(
            "/api/settings/smart-routing",
            json={"enabled": True, "model": "not/enabled"},
            headers=headers,
        )
        assert denied.status_code == 400

        saved = client.put(
            "/api/settings/smart-routing",
            json={"enabled": True, "model": "anthropic/claude-3.7-sonnet"},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json() == {
            "enabled": True,
            "model": "anthropic/claude-3.7-sonnet",
        }
        fetched = client.get("/api/settings/smart-routing")
        assert fetched.status_code == 200
        assert fetched.json()["enabled"] is True
        assert fetched.json()["model"] == "anthropic/claude-3.7-sonnet"


def test_failed_smart_routing_save_keeps_the_previous_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_settings: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("app.ai.models_router.save_settings_yaml", boom)
    app, http_client, _captured = _setup_chat_app(tmp_path, "unused")
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        with pytest.raises(OSError, match="disk full"):
            client.put(
                "/api/settings/smart-routing",
                json={"enabled": True, "model": "anthropic/claude-3.7-sonnet"},
                headers=headers,
            )
        assert app.state.settings.smart_routing_enabled is False
        assert app.state.settings.smart_routing_model == ""


def test_send_chat_message_writing_profile_returns_anchored_hunk(tmp_path: Path) -> None:
    reply = (
        "Line 30 has a grammar error.\n\n"
        "<<<<<<< SEARCH\n"
        "We shows the result.\n"
        "=======\n"
        "We show the result.\n"
        ">>>>>>> REPLACE\n"
    )
    app, http_client, _captured = _setup_chat_app(tmp_path, reply)
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        project_id = client.post(
            "/api/projects", json={"name": "Anchored"}, headers=headers
        ).json()["project_id"]
        lines = [f"Filler line {i}." for i in range(1, 51)]
        lines[29] = "We shows the result."
        etag = client.get(f"/api/projects/{project_id}/files/main.tex").json()["etag"]
        write_res = client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={"content": "\n".join(lines) + "\n", "expected_etag": etag},
            headers=headers,
        )
        assert write_res.status_code == 200, write_res.text
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "T"}, headers=headers
        ).json()["chat_id"]

        res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/messages",
            json={
                "prompt": "Fix the grammar.",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "writing",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert res.status_code == 200
        hunks = res.json()["hunks"]
        assert len(hunks) == 1
        assert hunks[0]["start_line"] == 30
        assert hunks[0]["end_line"] == 30
        assert hunks[0]["original_text"] == "We shows the result."
        assert hunks[0]["replacement_text"] == "We show the result."


def _autofix_with_reply(tmp_path: Path, reply: str) -> dict:
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "ai-router-pass",
            "privacy_consent_accepted": True,
        }
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)

        def mock_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": reply}}],
                    "usage": {"prompt_tokens": 90, "completion_tokens": 45},
                },
            )

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(mock_handler),
            base_url="https://openrouter.ai/api/v1",
        )
        app.state.secrets_store.update_secrets(
            {"openrouter_api_key": "sk-or-v1-router-test-key"}
        )
        app.state.openrouter_client = OpenRouterClient(
            db=app.state.db,
            secrets=app.state.secrets_store,
            settings=app.state.settings,
            registry=app.state.model_registry,
            http_client=http_client,
        )
        app.state.chat_service.openrouter = app.state.openrouter_client

        project_id = client.post(
            "/api/projects", json={"name": "Autofix List"}, headers=headers
        ).json()["project_id"]
        res = client.post(
            f"/api/projects/{project_id}/compile/autofix",
            json={
                "diagnostic": {
                    "severity": "error",
                    "file_path": "main.tex",
                    "line": 1,
                    "message": "Undefined control sequence \\badcmd.",
                    "context_lines": "l.1 \\badcmd",
                    "missing_package": None,
                },
                "model": "anthropic/claude-3.7-sonnet",
            },
            headers=headers,
        )
        assert res.status_code == 200
        return res.json()


def test_compile_autofix_endpoint_returns_hunk_list(tmp_path: Path) -> None:
    first_line = "\\documentclass{article}"
    reply = (
        "Cause.\n\n<<<<<<< SEARCH\n"
        f"{first_line}\n=======\n{first_line}\n\\usepackage{{amssymb}}\n"
        ">>>>>>> REPLACE\n"
    )
    body = _autofix_with_reply(tmp_path, reply)
    assert "hunk" not in body
    assert len(body["hunks"]) == 1
    assert body["hunks"][0]["file_path"] == "main.tex"
    assert body["hunks"][0]["start_line"] == 1
    assert "amssymb" in body["hunks"][0]["replacement_text"]


def test_compile_autofix_endpoint_returns_empty_list_when_no_fix(tmp_path: Path) -> None:
    assert _autofix_with_reply(tmp_path, "I cannot fix this.") == {"hunks": []}


def _parse_sse_events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        event_name = ""
        data_line = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                data_line = line[len("data: "):]
        if event_name and data_line:
            events.append((event_name, json.loads(data_line)))
    return events


def test_start_run_streams_sse_events_and_persists_status_and_hunks(tmp_path: Path) -> None:
    replies = iter([
        json.dumps({"status": "Checking main.tex", "action": "answer"}),
        (
            "Here is the fix.\n\n"
            "<<<<<<< SEARCH\n"
            "We shows the result.\n"
            "=======\n"
            "We show the result.\n"
            ">>>>>>> REPLACE\n"
        ),
    ])
    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "ai-router-pass",
            "privacy_consent_accepted": True,
        }
    )
    app = create_app(settings=settings)

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": next(replies)}}],
                "usage": {"prompt_tokens": 90, "completion_tokens": 45},
            },
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_handler),
        base_url="https://openrouter.ai/api/v1",
    )

    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        project_id = client.post(
            "/api/projects", json={"name": "Runs Study"}, headers=headers
        ).json()["project_id"]
        etag = client.get(f"/api/projects/{project_id}/files/main.tex").json()["etag"]
        write_res = client.put(
            f"/api/projects/{project_id}/files/main.tex",
            json={"content": "We shows the result.\n", "expected_etag": etag},
            headers=headers,
        )
        assert write_res.status_code == 200
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "Run Chat"}, headers=headers
        ).json()["chat_id"]

        start_res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/runs",
            json={
                "prompt": "Fix the grammar.",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "writing",
                "active_file": "main.tex",
            },
            headers=headers,
        )
        assert start_res.status_code == 201
        run_meta = start_res.json()
        assert run_meta["project_id"] == project_id
        assert run_meta["chat_id"] == chat_id
        assert run_meta["profile_id"] == "writing"
        assert run_meta["status"] in {"running", "completed"}
        run_id = run_meta["run_id"]

        events_res = client.get(f"/api/ai/runs/{run_id}/events")
        assert events_res.status_code == 200
        assert events_res.headers["content-type"].startswith("text/event-stream")
        assert events_res.headers["cache-control"] == "no-cache"

        parsed = _parse_sse_events(events_res.text)
        event_types = [name for name, _ in parsed]
        assert "started" in event_types
        assert "status" in event_types
        assert "done" in event_types

        status_payloads = [payload for name, payload in parsed if name == "status"]
        assert any(p.get("pill_file") == "main.tex" for p in status_payloads)

        done_payload = next(payload for name, payload in parsed if name == "done")
        assert len(done_payload["hunks"]) == 1
        assert done_payload["hunks"][0]["replacement_text"] == "We show the result."

        msgs_res = client.get(f"/api/projects/{project_id}/chats/{chat_id}/messages")
        assert msgs_res.status_code == 200
        msgs = msgs_res.json()["messages"]
        assert [m["role"] for m in msgs] == ["user", "status", "status", "assistant"]
        assert msgs[1]["pill_file"] is None
        assert msgs[2]["pill_file"] == "main.tex"


def test_start_run_returns_409_when_another_run_is_active(tmp_path: Path) -> None:
    import asyncio
    import threading

    app, http_client, _ = _setup_chat_app(tmp_path, "unused")
    release = threading.Event()

    async def slow_run_agent_turn(*args, **kwargs):
        while not release.is_set():
            await asyncio.sleep(0.01)
        raise asyncio.CancelledError

    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        app.state.chat_service.run_agent_turn = slow_run_agent_turn

        project_id = client.post(
            "/api/projects", json={"name": "Conflict"}, headers=headers
        ).json()["project_id"]
        chat1 = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "C1"}, headers=headers
        ).json()["chat_id"]
        chat2 = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "C2"}, headers=headers
        ).json()["chat_id"]

        res1 = client.post(
            f"/api/projects/{project_id}/chats/{chat1}/runs",
            json={"prompt": "Hello", "model": "anthropic/claude-3.7-sonnet", "profile_id": "writing"},
            headers=headers,
        )
        assert res1.status_code == 201
        run_id = res1.json()["run_id"]

        res2 = client.post(
            f"/api/projects/{project_id}/chats/{chat2}/runs",
            json={"prompt": "Second", "model": "anthropic/claude-3.7-sonnet", "profile_id": "writing"},
            headers=headers,
        )
        assert res2.status_code == 409

        release.set()
        stop_res = client.post(f"/api/ai/runs/{run_id}/stop", headers=headers)
        assert stop_res.status_code == 200


def test_stop_run_endpoint_cancels_in_flight_run_and_keeps_status_lines(tmp_path: Path) -> None:
    import asyncio
    import time

    settings = bootstrap_data_dir(tmp_path / "data").model_copy(
        update={
            "workspace_password": "ai-router-pass",
            "privacy_consent_accepted": True,
        }
    )
    app = create_app(settings=settings)
    call_count = 0

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"status": "Reading main.tex", "action": "answer"}
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                },
            )
        # Second call stays in flight until the run is cancelled
        while True:
            await asyncio.sleep(0.05)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_handler),
        base_url="https://openrouter.ai/api/v1",
    )

    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        project_id = client.post(
            "/api/projects", json={"name": "StopRun"}, headers=headers
        ).json()["project_id"]
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "Stop"}, headers=headers
        ).json()["chat_id"]

        start_res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/runs",
            json={"prompt": "Stop me", "model": "anthropic/claude-3.7-sonnet", "profile_id": "writing"},
            headers=headers,
        )
        assert start_res.status_code == 201
        run_id = start_res.json()["run_id"]

        # Wait until the status event is emitted before stopping
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            active_res = client.get("/api/ai/runs/active")
            active_run = active_res.json()["run"]
            if active_run and any(e.get("type") == "status" for e in active_run["events"]):
                break
            time.sleep(0.01)

        stop_res = client.post(f"/api/ai/runs/{run_id}/stop", headers=headers)
        assert stop_res.status_code == 200
        assert stop_res.json() == {"run_id": run_id, "status": "stopped"}

        msgs = client.get(f"/api/projects/{project_id}/chats/{chat_id}/messages").json()["messages"]
        assert [m["role"] for m in msgs] == ["user", "status"]
        assert msgs[1]["content"] == "Reading main.tex"

        # A new run can start afterwards
        replies = iter([
            json.dumps({"status": "Checking main.tex", "action": "answer"}),
            "All good.",
        ])
        http_client2 = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _req: httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": next(replies)}}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                    },
                )
            ),
            base_url="https://openrouter.ai/api/v1",
        )
        _install_mock_openrouter(app, http_client2)
        next_res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/runs",
            json={"prompt": "Try again", "model": "anthropic/claude-3.7-sonnet", "profile_id": "writing"},
            headers=headers,
        )
        assert next_res.status_code == 201
        next_events = client.get(f"/api/ai/runs/{next_res.json()['run_id']}/events")
        assert next_events.status_code == 200
        next_parsed = _parse_sse_events(next_events.text)
        assert "done" in [name for name, _ in next_parsed]


def test_active_run_endpoint_and_auth_csrf_checks(tmp_path: Path) -> None:
    import asyncio
    import threading

    app, http_client, _ = _setup_chat_app(tmp_path, "unused")
    release = threading.Event()

    async def slow_run_agent_turn(*args, **kwargs):
        while not release.is_set():
            await asyncio.sleep(0.01)
        raise asyncio.CancelledError

    with TestClient(app) as client:
        # Unauthenticated checks -> 401
        assert client.get("/api/ai/runs/active").status_code == 401
        assert client.get("/api/ai/runs/unknown/events").status_code == 401
        assert client.post("/api/ai/runs/unknown/stop").status_code == 401
        assert client.post("/api/projects/p1/chats/c1/runs", json={"prompt": "hi"}).status_code == 401

        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)

        # Missing CSRF checks -> 403
        assert client.post("/api/projects/p1/chats/c1/runs", json={"prompt": "hi"}).status_code == 403
        assert client.post("/api/ai/runs/unknown/stop").status_code == 403

        # Idle active run -> {"run": None}
        idle_res = client.get("/api/ai/runs/active")
        assert idle_res.status_code == 200
        assert idle_res.json() == {"run": None}

        # Unknown run_id -> 404
        assert client.get("/api/ai/runs/unknown/events").status_code == 404
        assert client.post("/api/ai/runs/unknown/stop", headers=headers).status_code == 404

        project_id = client.post(
            "/api/projects", json={"name": "ActiveAuth"}, headers=headers
        ).json()["project_id"]
        chat_id = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "A"}, headers=headers
        ).json()["chat_id"]

        # Non-chat profile -> 400 before starting run
        bad_profile_res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/runs",
            json={
                "prompt": "Fix",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "compile_autofix",
            },
            headers=headers,
        )
        assert bad_profile_res.status_code == 400
        assert client.get("/api/ai/runs/active").json() == {"run": None}

        # Active run appears on GET /api/ai/runs/active
        app.state.chat_service.run_agent_turn = slow_run_agent_turn
        start_res = client.post(
            f"/api/projects/{project_id}/chats/{chat_id}/runs",
            json={
                "prompt": "Work",
                "model": "anthropic/claude-3.7-sonnet",
                "profile_id": "writing",
            },
            headers=headers,
        )
        assert start_res.status_code == 201
        run_id = start_res.json()["run_id"]

        active_res = client.get("/api/ai/runs/active")
        assert active_res.status_code == 200
        active_body = active_res.json()["run"]
        assert active_body is not None
        assert active_body["run_id"] == run_id
        assert active_body["project_id"] == project_id
        assert active_body["chat_id"] == chat_id
        assert active_body["profile_id"] == "writing"
        assert active_body["status"] == "running"
        assert isinstance(active_body["events"], list)

        release.set()
        assert client.post(f"/api/ai/runs/{run_id}/stop", headers=headers).status_code == 200
        assert client.get("/api/ai/runs/active").json() == {"run": None}


def test_pin_rename_and_delete_chat(tmp_path: Path) -> None:
    app, http_client, _captured = _setup_chat_app(tmp_path, "Reply.")
    with TestClient(app) as client:
        headers = _login_and_get_csrf(client)
        _install_mock_openrouter(app, http_client)
        project_id = client.post(
            "/api/projects", json={"name": "Chat Mgmt"}, headers=headers
        ).json()["project_id"]

        c1 = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "First Chat"}, headers=headers
        ).json()
        c2 = client.post(
            f"/api/projects/{project_id}/chats", json={"title": "Second Chat"}, headers=headers
        ).json()
        assert c1["pinned"] is False
        assert c2["pinned"] is False

        # Initially c2 is newer so it comes first
        listed = client.get(f"/api/projects/{project_id}/chats").json()["chats"]
        assert [c["chat_id"] for c in listed] == [c2["chat_id"], c1["chat_id"]]

        # Pin c1 -> c1 moves to the top
        pin_res = client.patch(
            f"/api/projects/{project_id}/chats/{c1['chat_id']}",
            json={"pinned": True},
            headers=headers,
        )
        assert pin_res.status_code == 200
        assert pin_res.json()["pinned"] is True
        listed_pinned = client.get(f"/api/projects/{project_id}/chats").json()["chats"]
        assert [c["chat_id"] for c in listed_pinned] == [c1["chat_id"], c2["chat_id"]]

        # Rename c1
        rename_res = client.patch(
            f"/api/projects/{project_id}/chats/{c1['chat_id']}",
            json={"title": "  Renamed Chat  "},
            headers=headers,
        )
        assert rename_res.status_code == 200
        assert rename_res.json()["title"] == "Renamed Chat"
        assert rename_res.json()["pinned"] is True

        # Empty title is rejected
        bad_rename = client.patch(
            f"/api/projects/{project_id}/chats/{c1['chat_id']}",
            json={"title": "   "},
            headers=headers,
        )
        assert bad_rename.status_code == 400

        # Unpin c1
        unpin_res = client.patch(
            f"/api/projects/{project_id}/chats/{c1['chat_id']}",
            json={"pinned": False},
            headers=headers,
        )
        assert unpin_res.status_code == 200
        assert unpin_res.json()["pinned"] is False

        # Upload an attachment to c1, then delete c1 and verify cleanup
        att_res = client.post(
            f"/api/projects/{project_id}/chats/{c1['chat_id']}/attachments",
            files={"file": ("notes.txt", b"hello", "text/plain")},
            headers=headers,
        )
        assert att_res.status_code == 201
        att_dir = app.state.chat_service.attachment_dir(project_id, c1["chat_id"])
        assert att_dir.is_dir()

        del_res = client.delete(
            f"/api/projects/{project_id}/chats/{c1['chat_id']}",
            headers=headers,
        )
        assert del_res.status_code == 204
        assert not att_dir.exists()

        remaining = client.get(f"/api/projects/{project_id}/chats").json()["chats"]
        assert [c["chat_id"] for c in remaining] == [c2["chat_id"]]

        # Missing chat returns 404 for both patch and delete
        assert (
            client.patch(
                f"/api/projects/{project_id}/chats/chat_missing",
                json={"pinned": True},
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.delete(
                f"/api/projects/{project_id}/chats/chat_missing",
                headers=headers,
            ).status_code
            == 404
        )
