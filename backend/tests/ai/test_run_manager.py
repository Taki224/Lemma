from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.ai.chat_service import (
    ChatTurnResponse,
    InvalidChatProfileError,
    ProposedHunk,
)
from app.ai.openrouter import (
    ModelNotAllowedError,
    OpenRouterError,
    PrivacyConsentRequiredError,
    SpendCapExceededError,
)
from app.ai.run_manager import ChatRunManager, RunConflictError


def _make_hunk(hunk_id: str = "h1") -> ProposedHunk:
    return ProposedHunk(
        hunk_id=hunk_id,
        file_path="main.tex",
        base_etag="etag-1",
        start_line=1,
        end_line=2,
        original_text="old",
        replacement_text="new",
    )


def _start_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "project_id": "proj-1",
        "chat_id": "chat-1",
        "user_prompt": "Improve the abstract.",
        "model": "openai/gpt-4o",
        "profile_id": "writing",
        "active_file": "main.tex",
        "selection": None,
        "cursor_line": 10,
        "buffer_text": "\\section{Intro}",
        "references": None,
        "reference_buffers": None,
        "attachments": None,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_start_run_rejects_concurrent_run_with_conflict() -> None:
    gate = asyncio.Event()
    started = asyncio.Event()

    async def fake_run_agent_turn(*args: Any, **kwargs: Any) -> ChatTurnResponse:
        started.set()
        await gate.wait()
        return ChatTurnResponse(
            chat_id="chat-1",
            user_message_id="u1",
            assistant_message_id="a1",
            reply="Done",
            model="openai/gpt-4o",
            profile_id="writing",
            hunks=[],
            context=[],
        )

    chat_service = MagicMock()
    chat_service.run_agent_turn = fake_run_agent_turn

    manager = ChatRunManager()
    state = manager.start_run(chat_service, **_start_kwargs())
    await started.wait()

    assert manager.get_active_run() is state
    assert manager.get_run(state.run_id) is state

    with pytest.raises(RunConflictError):
        manager.start_run(chat_service, **_start_kwargs(chat_id="chat-2"))

    gate.set()
    assert state.task is not None
    await state.task
    assert manager.get_active_run() is None


@pytest.mark.asyncio
async def test_subscribe_replays_past_events_and_streams_live_until_done() -> None:
    first_emit_done = asyncio.Event()
    allow_finish = asyncio.Event()

    async def fake_run_agent_turn(*args: Any, on_event: Any = None, **kwargs: Any) -> ChatTurnResponse:
        await on_event(
            {
                "type": "started",
                "chat_id": "chat-1",
                "user_message_id": "u1",
                "profile_id": "writing",
                "context": [{"path": "main.tex", "detail": "lines 1-10", "kind": "active"}],
            }
        )
        await on_event(
            {
                "type": "status",
                "message_id": "s1",
                "content": "Checking introduction",
                "pill_file": None,
            }
        )
        first_emit_done.set()
        await allow_finish.wait()
        await on_event(
            {
                "type": "status",
                "message_id": "s2",
                "content": "Proposed an edit to main.tex",
                "pill_file": "main.tex",
            }
        )
        return ChatTurnResponse(
            chat_id="chat-1",
            user_message_id="u1",
            assistant_message_id="a1",
            reply="Updated the abstract.",
            model="openai/gpt-4o",
            profile_id="writing",
            hunks=[_make_hunk()],
            context=[{"path": "main.tex", "detail": "lines 1-10", "kind": "active"}],
        )

    chat_service = MagicMock()
    chat_service.run_agent_turn = fake_run_agent_turn

    manager = ChatRunManager()
    state = manager.start_run(chat_service, **_start_kwargs())
    await first_emit_done.wait()

    received: list[dict[str, Any]] = []

    async def consume() -> None:
        async for evt in manager.subscribe(state.run_id):
            received.append(evt)

    consumer_task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    allow_finish.set()
    await consumer_task

    assert [e["type"] for e in received] == ["started", "status", "status", "done"]
    done_evt = received[-1]
    assert done_evt["run_id"] == state.run_id
    assert done_evt["project_id"] == "proj-1"
    assert done_evt["chat_id"] == "chat-1"
    assert done_evt["profile_id"] == "writing"
    assert done_evt["reply"] == "Updated the abstract."
    assert len(done_evt["hunks"]) == 1
    assert done_evt["hunks"][0]["hunk_id"] == "h1"
    assert state.status == "completed"

    # Subscribing after completion replays all events and exits cleanly.
    replayed = [evt async for evt in manager.subscribe(state.run_id)]
    assert [e["type"] for e in replayed] == ["started", "status", "status", "done"]


@pytest.mark.asyncio
async def test_stop_run_cancels_in_flight_task_and_emits_stopped() -> None:
    started = asyncio.Event()

    async def fake_run_agent_turn(*args: Any, on_event: Any = None, **kwargs: Any) -> ChatTurnResponse:
        await on_event(
            {
                "type": "started",
                "chat_id": "chat-1",
                "user_message_id": "u1",
                "profile_id": "writing",
                "context": [],
            }
        )
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("Should have been cancelled")

    chat_service = MagicMock()
    chat_service.run_agent_turn = fake_run_agent_turn

    manager = ChatRunManager()
    state = manager.start_run(chat_service, **_start_kwargs())
    await started.wait()

    received: list[dict[str, Any]] = []

    async def consume() -> None:
        async for evt in manager.subscribe(state.run_id):
            received.append(evt)

    consumer_task = asyncio.create_task(consume())
    await asyncio.sleep(0)

    stopped_state = await manager.stop_run(state.run_id)
    await consumer_task

    assert stopped_state.status == "stopped"
    assert [e["type"] for e in received] == ["started", "stopped"]
    assert received[-1]["run_id"] == state.run_id
    assert received[-1]["chat_id"] == "chat-1"
    assert manager.get_active_run() is None

    with pytest.raises(KeyError):
        await manager.stop_run("nonexistent-run")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected_code"),
    [
        (SpendCapExceededError("Daily cap reached"), 429),
        (PrivacyConsentRequiredError("Consent required"), 403),
        (ModelNotAllowedError("Model disabled"), 403),
        (PermissionError("Forbidden"), 403),
        (InvalidChatProfileError("Bad profile"), 400),
        (KeyError("missing-project"), 404),
        (OpenRouterError("Upstream 502"), 502),
    ],
)
async def test_run_manager_maps_spend_cap_and_privacy_errors_to_error_event(
    exc: Exception,
    expected_code: int,
) -> None:
    async def failing_run_agent_turn(*args: Any, **kwargs: Any) -> ChatTurnResponse:
        raise exc

    chat_service = MagicMock()
    chat_service.run_agent_turn = failing_run_agent_turn

    manager = ChatRunManager()
    state = manager.start_run(chat_service, **_start_kwargs())
    assert state.task is not None
    await state.task

    assert state.status == "failed"
    assert state.error_status_code == expected_code
    assert state.error_detail
    assert state.events[-1]["type"] == "error"
    assert state.events[-1]["status_code"] == expected_code
    assert state.events[-1]["detail"] == state.error_detail


@pytest.mark.asyncio
async def test_stop_all_cancels_active_run_on_shutdown() -> None:
    started = asyncio.Event()

    async def hanging_run_agent_turn(*args: Any, **kwargs: Any) -> ChatTurnResponse:
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("Should have been cancelled")

    chat_service = MagicMock()
    chat_service.run_agent_turn = hanging_run_agent_turn

    manager = ChatRunManager()
    state = manager.start_run(chat_service, **_start_kwargs())
    await started.wait()

    await manager.stop_all()

    assert state.status == "stopped"
    assert manager.get_active_run() is None


@pytest.mark.asyncio
async def test_stop_run_before_task_steps_finalizes_stopped_and_releases_lock() -> None:
    async def never_reached_run(*args: Any, **kwargs: Any) -> ChatTurnResponse:
        raise AssertionError("Should be cancelled before starting")

    chat_service = MagicMock()
    chat_service.run_agent_turn = never_reached_run

    manager = ChatRunManager()
    state = manager.start_run(chat_service, **_start_kwargs())

    # Cancel immediately before the scheduled task has had a chance to step into _execute_run.
    stopped_state = await manager.stop_run(state.run_id)

    assert stopped_state.status == "stopped"
    assert [e["type"] for e in stopped_state.events] == ["stopped"]
    assert manager.get_active_run() is None

    replayed = [evt async for evt in manager.subscribe(state.run_id)]
    assert [e["type"] for e in replayed] == ["stopped"]

    # Global lock is released: a subsequent run can start and be stopped via stop_all before stepping.
    state2 = manager.start_run(chat_service, **_start_kwargs(chat_id="chat-2"))
    await manager.stop_all()
    assert state2.status == "stopped"
    assert [e["type"] for e in state2.events] == ["stopped"]
    assert manager.get_active_run() is None


@pytest.mark.asyncio
async def test_subscribe_unknown_run_raises_key_error() -> None:
    manager = ChatRunManager()
    with pytest.raises(KeyError):
        async for _ in manager.subscribe("nonexistent-run"):
            pass


@pytest.mark.asyncio
async def test_prune_completed_runs_keeps_at_most_20_runs() -> None:
    async def quick_run(*args: Any, **kwargs: Any) -> ChatTurnResponse:
        return ChatTurnResponse(
            chat_id="chat-1",
            user_message_id="u1",
            assistant_message_id="a1",
            reply="Done",
            model="openai/gpt-4o",
            profile_id="writing",
            hunks=[],
            context=[],
        )

    chat_service = MagicMock()
    chat_service.run_agent_turn = quick_run

    manager = ChatRunManager()
    run_ids: list[str] = []
    for i in range(25):
        state = manager.start_run(chat_service, **_start_kwargs(chat_id=f"chat-{i}"))
        assert state.task is not None
        await state.task
        run_ids.append(state.run_id)

    for evicted_id in run_ids[:5]:
        assert manager.get_run(evicted_id) is None
    for kept_id in run_ids[5:]:
        assert manager.get_run(kept_id) is not None
