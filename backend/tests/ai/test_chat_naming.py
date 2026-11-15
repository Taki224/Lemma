from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.ai.chat_naming import parse_chat_title
from app.ai.chat_service import ChatService
from app.ai.model_registry import ModelRegistry, build_seed_models
from app.ai.openrouter import DEFAULT_FALLBACK_MODELS, OpenRouterClient
from app.ai.prompts import PromptsManager
from app.config import bootstrap_data_dir
from app.config_secrets import SecretsStore
from app.db import DatabaseManager
from app.projects.service import ProjectService


def _setup_naming_service(
    tmp_path: Path,
    *,
    naming_enabled: bool = True,
    naming_model: str = "openai/gpt-4o",
    naming_response: str = "Proofread Introduction Section",
    naming_status: int = 200,
) -> tuple[ChatService, ProjectService, list[dict[str, Any]]]:
    settings = bootstrap_data_dir(tmp_path / "data")
    settings.privacy_consent_accepted = True
    settings.chat_naming_enabled = naming_enabled
    settings.chat_naming_model = naming_model
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()
    secrets = SecretsStore(secrets_path=settings.secrets_yaml_path)
    secrets.update_secrets({"openrouter_api_key": "sk-or-v1-test-naming-key"})
    prompts = PromptsManager(prompts_path=settings.prompts_yaml_path)
    project_service = ProjectService(data_dir=settings.data_dir, db=db)

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured.append(body)
        system_msg = (body.get("messages") or [{}])[0].get("content", "")
        if "short, descriptive title" in system_msg:
            if naming_status >= 400:
                return httpx.Response(naming_status, json={"error": "naming failed"})
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": naming_response}}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 5},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Main assistant reply."}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 10},
            },
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://openrouter.ai/api/v1",
    )
    registry = ModelRegistry(db)
    registry.seed_if_needed(build_seed_models([], DEFAULT_FALLBACK_MODELS))
    openrouter = OpenRouterClient(
        db=db,
        secrets=secrets,
        settings=settings,
        registry=registry,
        http_client=http_client,
    )
    chat_service = ChatService(
        db=db,
        project_service=project_service,
        prompts_manager=prompts,
        openrouter=openrouter,
        settings=settings,
    )
    return chat_service, project_service, captured


def test_parse_chat_title_normalizes_and_truncates() -> None:
    assert parse_chat_title('  "Quantum Error Correction." \n') == "Quantum Error Correction"
    assert parse_chat_title("   ") is None
    long_title = "word " * 30
    parsed = parse_chat_title(long_title)
    assert parsed is not None
    assert len(parsed) <= 80


@pytest.mark.asyncio
async def test_send_message_names_chat_on_initial_prompt_only(tmp_path: Path) -> None:
    chat_service, project_service, captured = _setup_naming_service(tmp_path)
    project = project_service.create_project("Paper")
    chat = chat_service.create_chat(project.project_id, title="New Chat")

    await chat_service.send_message(
        project_id=project.project_id,
        chat_id=chat.chat_id,
        user_prompt="Please proofread the introduction section of my paper.",
        model="openai/gpt-4o",
    )

    chats = chat_service.list_chats(project.project_id)
    assert chats[0].title == "Proofread Introduction Section"
    assert len(captured) == 2

    # Second prompt in the same chat should not invoke the naming model again.
    await chat_service.send_message(
        project_id=project.project_id,
        chat_id=chat.chat_id,
        user_prompt="Now check the conclusion.",
        model="openai/gpt-4o",
    )
    chats_after = chat_service.list_chats(project.project_id)
    assert chats_after[0].title == "Proofread Introduction Section"
    assert len(captured) == 3


@pytest.mark.asyncio
async def test_run_agent_turn_names_chat_and_ignores_naming_failure(tmp_path: Path) -> None:
    chat_service, project_service, _ = _setup_naming_service(
        tmp_path / "ok",
        naming_response="Fix Equation Formatting",
    )
    project = project_service.create_project("Manuscript")

    await chat_service.run_agent_turn(
        project_id=project.project_id,
        chat_id="chat_agent_1",
        user_prompt="Can you fix the equation alignment?",
        model="openai/gpt-4o",
    )
    assert chat_service.list_chats(project.project_id)[0].title == "Fix Equation Formatting"

    failing_service, failing_projects, _ = _setup_naming_service(
        tmp_path / "fail",
        naming_status=500,
    )
    p2 = failing_projects.create_project("Fallback")
    turn = await failing_service.run_agent_turn(
        project_id=p2.project_id,
        chat_id="chat_agent_2",
        user_prompt="Original prompt title",
        model="openai/gpt-4o",
    )
    assert turn.reply == "Main assistant reply."
    assert failing_service.list_chats(p2.project_id)[0].title == "Original prompt title"
