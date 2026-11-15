from __future__ import annotations

from app.ai.chat_service import _SEARCH_REPLACE_RE
from app.ai.output_contracts import (
    AGENT_STEP_CONTRACT,
    CHAT_EDIT_CONTRACT,
    PRECEDENCE_LINE,
    QUICK_EDIT_CONTRACT,
    compose_system_prompt,
)


def test_compose_puts_contract_last_after_instructions() -> None:
    result = compose_system_prompt("Be helpful.", QUICK_EDIT_CONTRACT)
    assert result.startswith("Be helpful.")
    assert result.endswith(QUICK_EDIT_CONTRACT)


def test_compose_includes_precedence_line() -> None:
    result = compose_system_prompt("Be helpful.", QUICK_EDIT_CONTRACT)
    assert PRECEDENCE_LINE == (
        "OUTPUT FORMAT — these rules override any formatting instructions above:"
    )
    assert result == f"Be helpful.\n\n{PRECEDENCE_LINE}\n{QUICK_EDIT_CONTRACT}"
    assert result.index("Be helpful.") < result.index(PRECEDENCE_LINE)
    assert result.index(PRECEDENCE_LINE) < result.index(QUICK_EDIT_CONTRACT)


def test_compose_strips_instruction_whitespace() -> None:
    result = compose_system_prompt("\n  Be helpful.  \n\n", QUICK_EDIT_CONTRACT)
    assert result.startswith("Be helpful.\n\n")


def test_chat_contract_example_matches_parser_regex() -> None:
    example = CHAT_EDIT_CONTRACT.replace(
        "(lines copied verbatim from the active file)", "\\section{Intro}"
    ).replace("(replacement lines)", "\\section{Introduction}")
    match = _SEARCH_REPLACE_RE.search(example)
    assert match is not None
    assert match.group(1) == "\\section{Intro}"
    assert match.group(2) == "\\section{Introduction}"


def test_agent_step_contract_composes_last_and_forbids_update_claims() -> None:
    profile_prompt = "You are a helpful LaTeX assistant."
    result = compose_system_prompt(profile_prompt, AGENT_STEP_CONTRACT)

    assert result == f"{profile_prompt}\n\n{PRECEDENCE_LINE}\n{AGENT_STEP_CONTRACT}"
    assert result.index(profile_prompt) < result.index(PRECEDENCE_LINE)
    assert result.index(PRECEDENCE_LINE) < result.index(AGENT_STEP_CONTRACT)
    assert result.endswith(f"{PRECEDENCE_LINE}\n{AGENT_STEP_CONTRACT}")

    lowered = AGENT_STEP_CONTRACT.lower()
    assert "json" in lowered
    for key in ('"status"', '"finding"', '"action"', '"read"', '"answer"', '"path"', '"focus"', '"start_line"', '"end_line"', '"pages"'):
        assert key in AGENT_STEP_CONTRACT
    assert "updated" in lowered
    assert "saved" in lowered
    assert "compiled" in lowered
    assert "never" in lowered


def test_agent_step_contract_mentions_search_action() -> None:
    assert '"search"' in AGENT_STEP_CONTRACT
    assert '"query"' in AGENT_STEP_CONTRACT
    assert '"path"' in AGENT_STEP_CONTRACT
