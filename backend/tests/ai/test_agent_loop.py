from __future__ import annotations

from app.ai.agent_loop import (
    MAX_AGENT_READS,
    MAX_STATUS_CHARS,
    STEP_MAX_TOKENS,
    AgentStepDecision,
    build_step_messages,
    parse_agent_step_decision,
    sanitize_status_line,
)
from app.ai.manuscript_context import ManuscriptContext
from app.ai.output_contracts import AGENT_STEP_CONTRACT, compose_system_prompt
from app.ai.prompts import PromptProfile
from app.ai.reference_context import ReferenceContext


def test_parse_agent_step_decision_read_with_line_slice_and_fences() -> None:
    raw = (
        "```json\n"
        "{\n"
        '  "status": "Checking methods section in sections/methods.tex",\n'
        '  "action": "read",\n'
        '  "path": "sections/methods.tex",\n'
        '  "focus": "ablation setup",\n'
        '  "start_line": 20,\n'
        '  "end_line": 80,\n'
        '  "pages": [2, 3]\n'
        "}\n"
        "```"
    )
    decision = parse_agent_step_decision(raw, is_initial=True, active_file="main.tex")
    assert decision == AgentStepDecision(
        status="Checking methods section in sections/methods.tex",
        action="read",
        path="sections/methods.tex",
        focus="ablation setup",
        start_line=20,
        end_line=80,
        pages=[2, 3],
    )


def test_parse_agent_step_decision_finding_field_on_followup() -> None:
    raw = (
        '{"status": "ignored fallback status", '
        '"finding": "Found ablation results in Table 2", '
        '"action": "answer"}'
    )
    decision = parse_agent_step_decision(raw, is_initial=False, active_file="main.tex")
    assert decision == AgentStepDecision(
        status="Found ablation results in Table 2",
        action="answer",
    )


def test_parse_agent_step_decision_malformed_or_missing_path_falls_back_to_answer() -> None:
    malformed = parse_agent_step_decision(
        "not json at all", is_initial=True, active_file="main.tex"
    )
    assert malformed.action == "answer"
    assert malformed.path is None
    assert "main.tex" in malformed.status

    missing_path = parse_agent_step_decision(
        '{"status": "Reading something", "action": "read", "path": "   "}',
        is_initial=False,
        active_file="main.tex",
    )
    assert missing_path.action == "answer"
    assert missing_path.path is None
    assert missing_path.status == "Reading something"


def test_sanitize_status_line_strips_newlines_and_truncates() -> None:
    long_text = "  Checking\n\nmultiple\tlines   " + ("x" * (MAX_STATUS_CHARS + 40))
    sanitized = sanitize_status_line(long_text, fallback="Fallback status")
    assert "\n" not in sanitized
    assert "\t" not in sanitized
    assert "  " not in sanitized
    assert len(sanitized) <= MAX_STATUS_CHARS
    assert sanitized.startswith("Checking multiple lines ")

    assert sanitize_status_line("   \n\t  ", fallback="Fallback status") == "Fallback status"


def test_build_step_messages_includes_file_list_working_notes_and_budget() -> None:
    profile = PromptProfile(
        id="writing",
        name="Writing",
        role="writing",
        system_prompt="You are a helpful LaTeX assistant.",
    )
    context = ManuscriptContext(
        mode="file",
        window_text="\\section{Intro}\nHello",
        window_start=1,
        window_end=2,
        parts=["Editable region (main.tex lines 1-2):\n\\section{Intro}\nHello"],
        quote_markers=("--- END ---",),
    )
    ref_context = ReferenceContext(
        parts=["Reference block (refs.bib):\n@article{doe2024}"],
        summaries=[],
        quote_markers=("--- END REF ---",),
    )
    history = [
        {"role": "user", "content": "Earlier question"},
        {"role": "assistant", "content": "Earlier answer"},
    ]

    messages = build_step_messages(
        profile=profile,
        history=history,
        context=context,
        ref_context=ref_context,
        project_files=["z_appendix.tex", "main.tex", "sections/methods.tex"],
        attachments=["notes.pdf"],
        working_notes=["Read sections/methods.tex lines 1-40:\nSample method text"],
        reads_used=2,
        max_reads=MAX_AGENT_READS,
        user_prompt="Verify the method description against the appendix.",
    )

    assert STEP_MAX_TOKENS == 1024
    assert MAX_AGENT_READS == 4
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == compose_system_prompt(
        profile.system_prompt, AGENT_STEP_CONTRACT
    )
    assert messages[1:-1] == history
    user_msg = messages[-1]
    assert user_msg["role"] == "user"
    content = user_msg["content"]

    # Sorted project files + attachment names
    assert "main.tex" in content
    assert "sections/methods.tex" in content
    assert "z_appendix.tex" in content
    assert content.index("main.tex") < content.index("sections/methods.tex") < content.index("z_appendix.tex")
    assert "notes.pdf" in content

    # Active context, ref context, working notes, budget, and user prompt
    assert "Editable region (main.tex lines 1-2):" in content
    assert "Reference block (refs.bib):" in content
    assert "Sample method text" in content
    assert f"2 of {MAX_AGENT_READS}" in content
    assert "Verify the method description against the appendix." in content


def test_parse_agent_step_decision_search_action() -> None:
    with_path = parse_agent_step_decision(
        '{"status": "Searching for theorem 2", "action": "search", '
        '"query": "Theorem 2", "path": "sections/methods.tex"}',
        is_initial=True,
        active_file="main.tex",
    )
    assert with_path == AgentStepDecision(
        status="Searching for theorem 2",
        action="search",
        query="Theorem 2",
        path="sections/methods.tex",
    )

    without_path = parse_agent_step_decision(
        '{"status": "Searching across project", "action": "search", "query": "  \\\\label{eq:loss}  "}',
        is_initial=False,
        active_file="main.tex",
    )
    assert without_path == AgentStepDecision(
        status="Searching across project",
        action="search",
        query="\\label{eq:loss}",
        path=None,
    )

    empty_query = parse_agent_step_decision(
        '{"status": "Searching", "action": "search", "query": "   ", "path": "main.tex"}',
        is_initial=True,
        active_file="main.tex",
    )
    assert empty_query.action == "answer"
    assert empty_query.query == ""
    assert empty_query.path is None

    missing_query = parse_agent_step_decision(
        '{"status": "Searching", "action": "search"}',
        is_initial=False,
        active_file="main.tex",
    )
    assert missing_query.action == "answer"
    assert missing_query.query == ""


def test_build_step_messages_index_mode_and_search_working_notes() -> None:
    profile = PromptProfile(
        id="writing",
        name="Writing",
        role="writing",
        system_prompt="You are a helpful LaTeX assistant.",
    )
    context = ManuscriptContext(
        mode="index",
        window_text="",
        window_start=1,
        window_end=1,
        parts=[
            "Project index (map only; inspect specific spans before editing):\nmain.tex",
            "Read-only root preamble (main.tex). Do not edit this block.\n\\documentclass{article}",
        ],
        quote_markers=("--- END ---",),
    )
    ref_context = ReferenceContext(
        parts=["Reference block (refs.bib):\n@article{doe2024}"],
        summaries=[],
        quote_markers=("--- END REF ---",),
    )

    messages = build_step_messages(
        profile=profile,
        history=[],
        context=context,
        ref_context=ref_context,
        project_files=["main.tex"],
        working_notes=["Search for 'ablation':\nsections/methods.tex:42: \\subsection{Ablation}"],
        reads_used=1,
        max_reads=MAX_AGENT_READS,
        user_prompt="Where is the ablation study?",
    )

    content = messages[-1]["content"]
    assert "Project index" in content
    assert "Read-only root preamble" in content
    assert "Reference block (refs.bib):" in content
    # In index mode, context.parts are not split around ref_context.parts
    assert content.index("Project index") < content.index("Read-only root preamble") < content.index("Reference block")
    assert "Search for 'ablation':" in content
    assert "sections/methods.tex:42: \\subsection{Ablation}" in content
