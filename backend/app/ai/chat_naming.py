from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import httpx

from app.ai.openrouter import (
    ModelNotAllowedError,
    OpenRouterError,
    PrivacyConsentRequiredError,
    SpendCapExceededError,
)

if TYPE_CHECKING:
    from app.ai.openrouter import OpenRouterClient
    from app.config import AppSettings

logger = logging.getLogger(__name__)

CHAT_NAMING_INSTRUCTION = (
    "Generate a short, descriptive title (3 to 6 words) for a chat session "
    "based on the user's initial prompt. "
    "Reply with only the title text — no quotes, no markdown, no trailing punctuation."
)

CHAT_NAMING_PROMPT_CHAR_LIMIT = 2000
CHAT_NAMING_MAX_TOKENS = 24
CHAT_NAMING_TITLE_MAX_LEN = 80


def chat_naming_messages(prompt: str) -> list[dict[str, str]]:
    """Build the completion messages for naming a new chat session."""
    excerpt = (prompt or "").strip()[:CHAT_NAMING_PROMPT_CHAR_LIMIT]
    return [
        {"role": "system", "content": CHAT_NAMING_INSTRUCTION},
        {"role": "user", "content": f"<user_prompt>\n{excerpt}\n</user_prompt>"},
    ]


def parse_chat_title(text: str) -> str | None:
    """Normalize a model-generated chat title, returning None if empty."""
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    cleaned = cleaned.strip("\"'`*_#")
    cleaned = cleaned.rstrip(".:;!")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if len(cleaned) > CHAT_NAMING_TITLE_MAX_LEN:
        cleaned = cleaned[:CHAT_NAMING_TITLE_MAX_LEN].rstrip()
    return cleaned or None


async def generate_chat_title(
    *,
    settings: AppSettings,
    openrouter: OpenRouterClient,
    prompt: str,
    session_id: str | None = None,
) -> str | None:
    """Call the configured chat-naming model to produce a short session title."""
    model = settings.chat_naming_model.strip()
    if not settings.chat_naming_enabled or not model or not prompt.strip():
        return None
    try:
        completion = await openrouter.complete(
            model=model,
            messages=chat_naming_messages(prompt),
            max_tokens=CHAT_NAMING_MAX_TOKENS,
            session_id=session_id,
        )
    except (
        PrivacyConsentRequiredError,
        SpendCapExceededError,
        ModelNotAllowedError,
        OpenRouterError,
        httpx.HTTPError,
    ) as exc:
        logger.warning(
            "Chat session naming failed (%s); keeping default title.",
            type(exc).__name__,
        )
        return None
    return parse_chat_title(completion.content)
