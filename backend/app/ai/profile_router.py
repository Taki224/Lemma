from __future__ import annotations

from collections.abc import Sequence
import re
from typing import Literal, cast

ChatProfileId = Literal[
    "writing",
    "synthesis",
    "proofreading",
    "tighten",
    "verify_claim",
    "formal_tone",
    "explain_math",
    "explain",
]

AUTO_PROFILE_ID = "auto"

# Task language, not topic words. Bare "grammar" or "literature" would catch a section title.
_EXPLAIN_MATH: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:explain|walk\s+(?:me\s+)?through|break\s+down)\b(?:\s+\w+){0,5}\s+"
        r"(?:math|mathematics|equation|equations|formula|formulas|derivation|proof|notation|integral|theorem|lemma|expression)\b",
        r"\bwhat\s+does\s+this\s+(?:equation|formula|expression|notation|symbol|math)\s+mean\b",
        r"\bstep[- ]by[- ]step\s+(?:math|derivation|equation)\b",
    )
)

_VERIFY_CLAIM: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:verify|fact[- ]?check|validate)\b(?:\s+\w+){0,5}\s+"
        r"(?:claim|claims|statement|statements|assertion|finding)\b",
        r"\bcheck\b(?:\s+\w+){0,4}\s+if\b(?:\s+\w+){0,4}\s+(?:claim|statement)\b",
        r"\bis\s+this\s+(?:claim|statement|assertion)\s+(?:supported|backed|verified|grounded|true|accurate)\b",
        r"\bdoes\s+the\s+(?:cited|source|paper|pdf)\b(?:\s+\w+){0,4}\s+(?:support|back|confirm)\b",
    )
)

_FORMAL_TONE: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bformal\s+(?:academic\s+)?tone\b",
        r"\b(?:make|rewrite|rephrase|change|put)\b(?:\s+\w+){0,6}\s+"
        r"(?:more\s+)?(?:formal|scholarly|academic|objective)\b",
        r"\bin\s+a\s+(?:more\s+)?(?:formal|scholarly|academic|objective)\s+(?:tone|style|voice|manner)\b",
    )
)

_TIGHTEN: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\btighten(?:\s+up)?\b",
        r"\b(?:make|rewrite)\b(?:\s+\w+){0,5}\s+"
        r"(?:more\s+)?(?:concise|shorter|tighter|briefer)\b",
        r"\b(?:shorten|condense|trim)\b",
        r"\b(?:for|to\s+meet|under)\s+(?:the\s+)?(?:page|word|line)\s+limits?\b",
        r"\breduce\s+(?:the\s+)?(?:word|line|page)\s+count\b",
    )
)

_PROOFREADING: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bproof-?reads?\b",
        r"\bproof\s+reads?\b",
        r"\bproof-?reading\b",
        r"\bcopy-?edits?\b",
        r"\bcopy\s+edits?\b",
        r"\bcopy-?editing\b",
        r"\bline-?edits?\b",
        r"\bline-?editing\b",
        r"\bspell(?:check|checker)\b",
        r"\bspell\s+checks?\b",
        r"\btypos?\b",
        r"\bgrammar and spelling\b",
        r"\bgrammar\s+checks?\b",
        r"\b(?:fix|check|correct|clean(?:\s+up)?)\b(?:\s+\w+){0,4}\s+"
        r"(?:grammar|spelling|typos?|punctuation|wording)\b",
    )
)

_SYNTHESIS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:write|draft|survey)\b(?:\s+\w+){0,4}\s+"
        r"(?:the\s+)?(?:related[- ]work|literature review|prior work)\b",
        r"\bwhat does the literature\b",
        r"\b(?:add|insert)s?\s+(?:a\s+)?citations?\b",
        r"\b(?:add|insert)s?\s+\\cite(?:p|t|alp|author)?\b",
        r"\bsynthesi[sz]e\b(?:\s+\w+){0,5}\s+"
        r"(?:papers|studies|sources|articles|findings|literature)\b",
        r"\bcompare\b(?:\s+\w+){0,6}\s+(?:papers|studies|sources|articles|findings)\b",
    )
)

_EXPLAIN: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\?\s*$",
        r"^\s*(?:what|why|how|which|when|where|who|does|did|is|are|can|could|should|would|will)\b",
        r"^\s*do\s+(?!not\b)(?:you|we|they|this|these|that|it)\b",
        r"\b(?:explain|clarify|elaborate\s+on|tell\s+me\s+about|help\s+me\s+understand)\b",
    )
)


def route_chat_profile(prompt: str, *, has_selection: bool = False) -> ChatProfileId:
    """Pick the chat profile for one prompt.

    Proofreading wins when the prompt asks for a language fix, even if it also
    mentions citations. Selection-focused actions (explain_math, verify_claim,
    formal_tone, tighten) trigger when a passage is selected, or on unambiguous
    math/claim verification requests. A synthesis task selects synthesis.
    General questions and explanation requests select explain.
    Everything else stays on drafting.
    """
    text = prompt or ""
    if any(pattern.search(text) for pattern in _PROOFREADING):
        return "proofreading"
    if any(pattern.search(text) for pattern in _EXPLAIN_MATH):
        return "explain_math"
    if any(pattern.search(text) for pattern in _VERIFY_CLAIM):
        return "verify_claim"
    if has_selection:
        if any(pattern.search(text) for pattern in _FORMAL_TONE):
            return "formal_tone"
        if any(pattern.search(text) for pattern in _TIGHTEN):
            return "tighten"
    if any(pattern.search(text) for pattern in _SYNTHESIS):
        return "synthesis"
    if any(pattern.search(text) for pattern in _EXPLAIN):
        return "explain"
    return "writing"


ROUTER_INSTRUCTION = (
    "Choose the profile for this academic writing request. "
    "Reply with exactly one of: writing, synthesis, proofreading, tighten, verify_claim, formal_tone, explain_math, explain.\n"
    "writing: draft or revise manuscript prose.\n"
    "synthesis: literature, citations, related work, or comparing sources.\n"
    "proofreading: grammar, spelling, typos, or copyediting.\n"
    "tighten: shorten or condense a passage to reduce word or page count.\n"
    "verify_claim: check whether a claim is supported by cited sources.\n"
    "formal_tone: rewrite a passage in a formal, objective academic tone.\n"
    "explain_math: explain mathematical notation, equations, or derivations step by step.\n"
    "explain: answer a question, explain a concept, methodology, experimental setup, or continue a discussion without editing manuscript prose.\n"
    "No other text."
)


ROUTER_PROMPT_CHAR_LIMIT = 4000
ROUTER_HISTORY_MAX_MESSAGES = 4
ROUTER_HISTORY_MESSAGE_CHARS = 400
ROUTER_MAX_TOKENS = 16

_VALID_ROUTED_PROFILES: frozenset[str] = frozenset(
    {
        "writing",
        "synthesis",
        "proofreading",
        "tighten",
        "verify_claim",
        "formal_tone",
        "explain_math",
        "explain",
    }
)


def parse_routed_profile(text: str) -> ChatProfileId | None:
    """Accept a reply that is only a profile name. Any other text falls back."""
    word = (text or "").strip().lower().strip(".,:;!\"'`")
    if word in _VALID_ROUTED_PROFILES:
        return cast(ChatProfileId, word)
    return None


def _format_router_history(history: Sequence[dict[str, str]] | None) -> str:
    if not history:
        return ""
    lines: list[str] = []
    for item in history[-ROUTER_HISTORY_MAX_MESSAGES:]:
        role = str(item.get("role") or "").strip().upper()
        content = str(item.get("content") or "").strip()
        if not role or not content:
            continue
        if len(content) > ROUTER_HISTORY_MESSAGE_CHARS:
            content = content[:ROUTER_HISTORY_MESSAGE_CHARS] + "..."
        lines.append(f"{role}: {content}")
    if not lines:
        return ""
    return "Recent conversation (for context only):\n" + "\n".join(lines) + "\n\n"


def router_messages(
    prompt: str,
    history: Sequence[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Build the routing completion.

    The manuscript prompt is wrapped as data and cut to ROUTER_PROMPT_CHAR_LIMIT
    so the model classifies it instead of following instructions inside it.
    """
    excerpt = (prompt or "").strip()[:ROUTER_PROMPT_CHAR_LIMIT]
    history_block = _format_router_history(history)
    return [
        {"role": "system", "content": ROUTER_INSTRUCTION},
        {"role": "user", "content": f"{history_block}Request to classify:\n{excerpt}"},
    ]


def resolve_chat_profile_id(
    profile_id: str | None,
    prompt: str,
    *,
    has_selection: bool = False,
) -> str:
    """Use an explicit profile id, or route when the caller asked for auto."""
    if profile_id is None or profile_id.strip().lower() in {"", AUTO_PROFILE_ID}:
        return route_chat_profile(prompt, has_selection=has_selection)
    return profile_id
