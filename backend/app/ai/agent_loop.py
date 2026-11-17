from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from app.ai.manuscript_context import ManuscriptContext
from app.ai.output_contracts import (
    AGENT_STEP_CONTRACT,
    AGENT_STEP_LITERATURE_CONTRACT,
    AGENT_STEP_WEB_CONTRACT,
    compose_system_prompt,
)
from app.ai.prompts import PromptProfile
from app.ai.reference_context import ReferenceContext

MAX_AGENT_READS = 4
MAX_EXTERNAL_SEARCHES = 3
STEP_MAX_TOKENS = 1024
STEP_EXTRA_BODY: dict[str, Any] = {"reasoning": {"effort": "minimal"}}
MAX_STATUS_CHARS = 160
LITERATURE_STATUS = "Searching the literature…"
WEB_STATUS = "Searching the web…"

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@dataclass(frozen=True)
class AgentStepDecision:
    """Structured decision returned by one planning/read step of the agent loop."""

    status: str
    action: Literal["search", "read", "answer", "find_papers", "web_search"]
    path: str | None = None
    query: str = ""
    focus: str = ""
    start_line: int | None = None
    end_line: int | None = None
    pages: list[int] | None = None


def sanitize_status_line(text: str, fallback: str) -> str:
    """Collapse whitespace/newlines to a single space, cap length, or use fallback."""
    cleaned = " ".join(text.split())
    if not cleaned:
        cleaned = " ".join(fallback.split())
    if len(cleaned) <= MAX_STATUS_CHARS:
        return cleaned
    return cleaned[:MAX_STATUS_CHARS].rstrip()


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    stripped = _FENCE_RE.sub("", raw.strip()).strip()
    if not stripped:
        return None
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _parse_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def _parse_pages(value: Any) -> list[int] | None:
    if not isinstance(value, list):
        return None
    pages = [item for item in value if isinstance(item, int) and not isinstance(item, bool) and item >= 1]
    return pages or None


def parse_agent_step_decision(
    raw: str,
    *,
    is_initial: bool,
    active_file: str,
) -> AgentStepDecision:
    """Parse a model step decision JSON object, falling back safely to 'answer'."""
    default_status = (
        f"Reviewing {active_file}" if is_initial and active_file else (
            "Preparing response" if is_initial else "Reviewed the requested material"
        )
    )
    fallback = sanitize_status_line(default_status, "Preparing response")

    data = _extract_json_object(raw)
    if data is None:
        return AgentStepDecision(status=fallback, action="answer")

    primary_key, secondary_key = ("status", "finding") if is_initial else ("finding", "status")
    raw_status = ""
    for key in (primary_key, secondary_key):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            raw_status = val
            break
    status = sanitize_status_line(raw_status, fallback)

    raw_action = data.get("action")
    action_str = raw_action.strip().lower() if isinstance(raw_action, str) else "answer"
    if action_str == "search":
        raw_query = data.get("query")
        query = raw_query.strip() if isinstance(raw_query, str) else ""
        if not query:
            return AgentStepDecision(status=status, action="answer")
        raw_path = data.get("path")
        path = raw_path.strip() if isinstance(raw_path, str) and raw_path.strip() else None
        return AgentStepDecision(
            status=status,
            action="search",
            path=path,
            query=query,
        )

    if action_str in {"find_papers", "web_search"}:
        raw_query = data.get("query")
        query = raw_query.strip() if isinstance(raw_query, str) else ""
        if not query:
            return AgentStepDecision(status=status, action="answer")
        fixed_status = LITERATURE_STATUS if action_str == "find_papers" else WEB_STATUS
        return AgentStepDecision(status=fixed_status, action=action_str, query=query)  # type: ignore[arg-type]

    if action_str != "read":
        return AgentStepDecision(status=status, action="answer")

    raw_path = data.get("path")
    path = raw_path.strip() if isinstance(raw_path, str) else ""
    if not path:
        return AgentStepDecision(status=status, action="answer")

    raw_focus = data.get("focus")
    focus = raw_focus.strip() if isinstance(raw_focus, str) else ""
    start_line = _parse_positive_int(data.get("start_line"))
    end_line = _parse_positive_int(data.get("end_line"))
    if start_line is not None and end_line is not None and end_line < start_line:
        end_line = None
    pages = _parse_pages(data.get("pages"))

    return AgentStepDecision(
        status=status,
        action="read",
        path=path,
        focus=focus,
        start_line=start_line,
        end_line=end_line,
        pages=pages,
    )


def build_step_messages(
    *,
    profile: PromptProfile,
    history: list[dict[str, str]],
    context: ManuscriptContext,
    ref_context: ReferenceContext,
    project_files: list[str],
    attachments: list[str] | None = None,
    working_notes: list[str] | None = None,
    reads_used: int = 0,
    max_reads: int = MAX_AGENT_READS,
    user_prompt: str,
    literature_search_enabled: bool = False,
    web_search_enabled: bool = False,
    searches_used: int = 0,
    max_searches: int = MAX_EXTERNAL_SEARCHES,
) -> list[dict[str, str]]:
    """Build chat messages for one planning/read step without mutating chat history."""
    contract = AGENT_STEP_CONTRACT
    if literature_search_enabled:
        contract += "\n" + AGENT_STEP_LITERATURE_CONTRACT
    if web_search_enabled:
        contract += "\n" + AGENT_STEP_WEB_CONTRACT
    system_content = compose_system_prompt(profile.system_prompt, contract)

    parts: list[str] = []
    if context.mode == "index":
        parts.extend(context.parts)
        parts.extend(ref_context.parts)
    else:
        if context.parts:
            parts.extend(context.parts[:-1])
        parts.extend(ref_context.parts)
        if context.parts:
            parts.append(context.parts[-1])

    sorted_files = sorted({p.strip() for p in project_files if p and p.strip()})
    file_lines = [f"- {path}" for path in sorted_files] or ["- (none)"]
    files_section = ["Available project files:", *file_lines]

    sorted_attachments = sorted({a.strip() for a in (attachments or []) if a and a.strip()})
    if sorted_attachments:
        files_section.append("Attached chat files:")
        files_section.extend(f"- {name}" for name in sorted_attachments)
    parts.append("\n".join(files_section))

    if working_notes:
        notes_body = "\n\n".join(note for note in working_notes if note.strip())
        if notes_body:
            parts.append(f"Working notes from searches and files already read:\n{notes_body}")

    remaining = max(0, max_reads - reads_used)
    parts.append(
        f"Read budget: {reads_used} of {max_reads} reads used ({remaining} remaining)."
    )
    if literature_search_enabled or web_search_enabled:
        searches_remaining = max(0, max_searches - searches_used)
        parts.append(
            f"External search budget (find_papers and web_search, separate from reads): "
            f"{searches_used} of {max_searches} used ({searches_remaining} remaining)."
        )
    parts.append(f"User request:\n{user_prompt}")

    return [
        {"role": "system", "content": system_content},
        *history,
        {"role": "user", "content": "\n\n".join(parts)},
    ]
