from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from app.ai.chat_service import ChatService, InvalidChatProfileError
from app.ai.openrouter import (
    ModelNotAllowedError,
    OpenRouterError,
    PrivacyConsentRequiredError,
    SpendCapExceededError,
)

logger = logging.getLogger(__name__)

_MAX_COMPLETED_RUNS = 20

_ERROR_STATUS_MAP: tuple[tuple[type[BaseException], int], ...] = (
    (InvalidChatProfileError, 400),
    (KeyError, 404),
    (PrivacyConsentRequiredError, 403),
    (ModelNotAllowedError, 403),
    (PermissionError, 403),
    (SpendCapExceededError, 429),
    (OpenRouterError, 502),
)


class RunConflictError(RuntimeError):
    """Raised when starting a chat run while another run is already active."""


@dataclass
class ChatRunState:
    run_id: str
    project_id: str
    chat_id: str
    model: str
    profile_id: str
    status: Literal["running", "completed", "stopped", "failed"] = "running"
    events: list[dict[str, Any]] = field(default_factory=list)
    hunks: list[dict[str, Any]] = field(default_factory=list)
    error_detail: str | None = None
    error_status_code: int | None = None
    created_at: float = field(default_factory=time.monotonic)
    created_seq: int = 0
    task: asyncio.Task[None] | None = None
    _subscribers: set[asyncio.Queue[dict[str, Any] | None]] = field(default_factory=set)


def _map_exception_to_http(exc: Exception) -> tuple[int, str]:
    """Map domain/provider exceptions to HTTP status codes and user-safe detail strings."""
    for exc_type, status_code in _ERROR_STATUS_MAP:
        if isinstance(exc, exc_type):
            detail = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            return status_code, str(detail) or exc_type.__name__
    return 500, str(exc) or "Internal server error"


class ChatRunManager:
    """Manages a single global active chat agent run with SSE replay and cancellation."""

    def __init__(self, *, max_completed_runs: int = _MAX_COMPLETED_RUNS) -> None:
        self._runs: dict[str, ChatRunState] = {}
        self._max_completed_runs = max_completed_runs
        self._seq = 0

    def get_active_run(self) -> ChatRunState | None:
        """Return the currently running chat run, if any."""
        for state in self._runs.values():
            if state.status == "running":
                return state
        return None

    def get_run(self, run_id: str) -> ChatRunState | None:
        """Return a tracked chat run by ID, or None if unknown or pruned."""
        return self._runs.get(run_id)

    def start_run(
        self,
        chat_service: ChatService,
        *,
        project_id: str,
        chat_id: str,
        user_prompt: str,
        model: str,
        profile_id: str,
        active_file: str | None,
        selection: dict[str, Any] | None,
        cursor_line: int | None,
        buffer_text: str | None,
        references: list[str] | None,
        reference_buffers: dict[str, str] | None,
        attachments: list[str] | None,
    ) -> ChatRunState:
        """Start a background agent run, enforcing one global active run at a time."""
        if self.get_active_run() is not None:
            raise RunConflictError("Another chat run is already in progress.")

        run_id = secrets.token_hex(12)
        self._seq += 1
        state = ChatRunState(
            run_id=run_id,
            project_id=project_id,
            chat_id=chat_id,
            model=model,
            profile_id=profile_id,
            created_seq=self._seq,
        )
        self._runs[run_id] = state
        state.task = asyncio.create_task(
            self._execute_run(
                state=state,
                chat_service=chat_service,
                user_prompt=user_prompt,
                active_file=active_file,
                selection=selection,
                cursor_line=cursor_line,
                buffer_text=buffer_text,
                references=references,
                reference_buffers=reference_buffers,
                attachments=attachments,
            )
        )
        return state

    async def stop_run(self, run_id: str) -> ChatRunState:
        """Cancel an active run and wait for its cleanup to finish."""
        state = self._runs.get(run_id)
        if state is None:
            raise KeyError(run_id)
        if state.status == "running" and state.task is not None:
            state.task.cancel()
            try:
                await state.task
            except asyncio.CancelledError:
                pass
            if state.status == "running":
                self._finalize_stopped(state)
        return state

    async def stop_all(self) -> None:
        """Cancel all active runs during application shutdown."""
        active_states: list[ChatRunState] = []
        for state in list(self._runs.values()):
            if state.status == "running" and state.task is not None:
                state.task.cancel()
                active_states.append(state)
        for state in active_states:
            assert state.task is not None
            try:
                await state.task
            except asyncio.CancelledError:
                pass
            if state.status == "running":
                self._finalize_stopped(state)

    async def subscribe(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        """Replay recorded events for run_id, then stream live events until completion."""
        state = self._runs.get(run_id)
        if state is None:
            raise KeyError(run_id)

        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        replay = list(state.events)
        is_terminal = state.status != "running"
        if not is_terminal:
            state._subscribers.add(queue)
        try:
            for event in replay:
                yield event
            if is_terminal:
                return
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item
        finally:
            state._subscribers.discard(queue)

    def _broadcast(self, state: ChatRunState, event: dict[str, Any]) -> None:
        state.events.append(event)
        for queue in list(state._subscribers):
            queue.put_nowait(event)

    def _close_subscribers(self, state: ChatRunState) -> None:
        for queue in list(state._subscribers):
            queue.put_nowait(None)

    def _finalize_stopped(self, state: ChatRunState) -> None:
        if state.status != "running":
            return
        state.status = "stopped"
        self._broadcast(
            state,
            {
                "type": "stopped",
                "run_id": state.run_id,
                "project_id": state.project_id,
                "chat_id": state.chat_id,
            },
        )
        self._close_subscribers(state)
        self._prune_completed_runs()

    def _prune_completed_runs(self) -> None:
        non_running = [
            (run_id, (state.created_seq, state.created_at))
            for run_id, state in self._runs.items()
            if state.status != "running"
        ]
        excess = len(non_running) - self._max_completed_runs
        if excess <= 0:
            return
        non_running.sort(key=lambda item: item[1])
        for run_id, _ in non_running[:excess]:
            self._runs.pop(run_id, None)

    async def _execute_run(
        self,
        *,
        state: ChatRunState,
        chat_service: ChatService,
        user_prompt: str,
        active_file: str | None,
        selection: dict[str, Any] | None,
        cursor_line: int | None,
        buffer_text: str | None,
        references: list[str] | None,
        reference_buffers: dict[str, str] | None,
        attachments: list[str] | None,
    ) -> None:
        async def on_event(event: dict[str, Any]) -> None:
            self._broadcast(state, event)

        try:
            result = await chat_service.run_agent_turn(
                project_id=state.project_id,
                chat_id=state.chat_id,
                user_prompt=user_prompt,
                model=state.model,
                profile_id=state.profile_id,
                active_file=active_file,
                selection=selection,
                cursor_line=cursor_line,
                buffer_text=buffer_text,
                references=references,
                reference_buffers=reference_buffers,
                attachments=attachments,
                on_event=on_event,
            )
            hunks_dicts = [asdict(h) for h in result.hunks]
            state.hunks = hunks_dicts
            state.status = "completed"
            self._broadcast(
                state,
                {
                    "type": "done",
                    "run_id": state.run_id,
                    "project_id": state.project_id,
                    "chat_id": result.chat_id,
                    "user_message_id": result.user_message_id,
                    "assistant_message_id": result.assistant_message_id,
                    "reply": result.reply,
                    "model": result.model,
                    "profile_id": result.profile_id,
                    "hunks": hunks_dicts,
                    "context": result.context,
                    "integrity": result.integrity,
                },
            )
        except asyncio.CancelledError:
            self._finalize_stopped(state)
        except Exception as exc:
            status_code, detail = _map_exception_to_http(exc)
            if status_code >= 500:
                logger.exception("Chat run %s failed unexpectedly", state.run_id)
            state.status = "failed"
            state.error_detail = detail
            state.error_status_code = status_code
            self._broadcast(
                state,
                {
                    "type": "error",
                    "run_id": state.run_id,
                    "project_id": state.project_id,
                    "chat_id": state.chat_id,
                    "detail": detail,
                    "error": detail,
                    "status_code": status_code,
                },
            )
        finally:
            self._close_subscribers(state)
            self._prune_completed_runs()
