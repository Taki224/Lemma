from __future__ import annotations

import pytest

from app.ai.profile_router import (
    ROUTER_HISTORY_MAX_MESSAGES,
    ROUTER_HISTORY_MESSAGE_CHARS,
    parse_routed_profile,
    resolve_chat_profile_id,
    route_chat_profile,
    router_messages,
)


@pytest.mark.parametrize(
    ("prompt", "has_selection", "expected"),
    [
        ("Draft the introduction from these notes", False, "writing"),
        ("Rewrite this paragraph to be more formal", False, "writing"),
        ("Rewrite this paragraph to be more formal", True, "formal_tone"),
        ("In a formal academic tone", True, "formal_tone"),
        ("Make this more objective", True, "formal_tone"),
        ("Polish the abstract", False, "writing"),
        ("", False, "writing"),
        ("   ", False, "writing"),
        ("Proofread this paragraph", False, "proofreading"),
        ("PROOFREAD the section", False, "proofreading"),
        ("Fix the grammar and spelling", False, "proofreading"),
        ("There are a few typos in the abstract", False, "proofreading"),
        ("Copyedit the conclusion", False, "proofreading"),
        ("Check spelling in the caption", False, "proofreading"),
        ("Spell check the abstract", False, "proofreading"),
        ("Proof read the caption", False, "proofreading"),
        ("Copy edit the conclusion", False, "proofreading"),
        ("Grammar check the conclusion", False, "proofreading"),
        ("Keep the British spelling", False, "writing"),
        ("Draft the punctuation section", False, "writing"),
        ("Correct the language model description", False, "writing"),
        ("Fix the grammar in the related work", False, "proofreading"),
        ("Compare the findings of the cited papers", False, "synthesis"),
        ("Add a citation for this claim", False, "synthesis"),
        ("Write the related work section", False, "synthesis"),
        ("What does the literature say about attention", False, "synthesis"),
        (r"Insert \cite{vaswani2017} here", False, "synthesis"),
        ("Synthesize the two studies", False, "synthesis"),
        ("Tighten the literature review", False, "writing"),
        ("Tighten this passage", True, "tighten"),
        ("Tighten for page limits", True, "tighten"),
        ("Make this paragraph shorter", True, "tighten"),
        ("Shorten the selected passage", True, "tighten"),
        ("Reduce the word count", True, "tighten"),
        ("Explain this equation", True, "explain_math"),
        ("Explain the math step by step", False, "explain_math"),
        ("What does this formula mean", True, "explain_math"),
        ("Verify this claim", True, "verify_claim"),
        ("Is this claim supported by the cited paper?", False, "verify_claim"),
        ("Fact-check the statement", True, "verify_claim"),
        ("A synthesis of ammonia under pressure", False, "writing"),
        ("Draft how plants synthesize glucose", False, "writing"),
        ("Do not cite the appendix", False, "writing"),
        ("Add a sentence after the citations", False, "writing"),
        ("Make the literature say less about transformers", False, "writing"),
        (r"Insert \citep{vaswani2017} here", False, "synthesis"),
        ("which models should i use for the tests", False, "explain"),
        ("i have a single NVIDIA L40S. Does that change anything?", False, "explain"),
        ("Explain how speculative decoding works", False, "explain"),
        (
            "Verify if this LaTeX compile issue exists and fix it:\n\n[error] main.tex:12 — Undefined control sequence",
            False,
            "fix_latex",
        ),
        (
            "Verify if this LaTeX compile issue exists and fix it:\n\n[warning] main.tex:? — Overfull \\hbox",
            False,
            "fix_latex",
        ),
        ("Fix this LaTeX error in main.tex", False, "fix_latex"),
        ("My document fails to compile with Missing $ inserted", False, "fix_latex"),
        ("Why won't my manuscript compile?", False, "fix_latex"),
        ("Undefined control sequence \\toprule", False, "fix_latex"),
        ("Package amsmath Error: \\begin{align} allowed only in paragraph mode.", False, "fix_latex"),
    ],
)
def test_route_chat_profile(prompt: str, has_selection: bool, expected: str) -> None:
    assert route_chat_profile(prompt, has_selection=has_selection) == expected


def test_resolve_keeps_an_explicit_profile() -> None:
    assert resolve_chat_profile_id("writing", "Proofread this paragraph") == "writing"
    assert resolve_chat_profile_id("synthesis", "Draft the introduction") == "synthesis"


def test_resolve_routes_auto_and_blank() -> None:
    assert resolve_chat_profile_id("auto", "Proofread this paragraph") == "proofreading"
    assert resolve_chat_profile_id(" AUTO ", "Draft the introduction") == "writing"
    assert resolve_chat_profile_id(None, "Add a citation") == "synthesis"
    assert resolve_chat_profile_id("  ", "Add a citation") == "synthesis"
    assert resolve_chat_profile_id("auto", "Tighten this", has_selection=True) == "tighten"
    assert resolve_chat_profile_id("auto", "Explain the math", has_selection=True) == "explain_math"
    assert resolve_chat_profile_id("auto", "Verify this claim", has_selection=True) == "verify_claim"
    assert (
        resolve_chat_profile_id("auto", "Rewrite in a formal academic tone", has_selection=True)
        == "formal_tone"
    )


def test_parse_routed_profile_reads_the_model_word() -> None:
    assert parse_routed_profile("synthesis") == "synthesis"
    assert parse_routed_profile("Proofreading.") == "proofreading"
    assert parse_routed_profile("tighten") == "tighten"
    assert parse_routed_profile("verify_claim") == "verify_claim"
    assert parse_routed_profile("formal_tone") == "formal_tone"
    assert parse_routed_profile("explain_math") == "explain_math"
    assert parse_routed_profile("explain") == "explain"
    assert parse_routed_profile("fix_latex") == "fix_latex"
    assert parse_routed_profile("Profile: Proofreading.") is None
    assert parse_routed_profile("writing: draft or revise manuscript prose.") is None
    assert parse_routed_profile("I am not sure") is None
    assert parse_routed_profile("writing, synthesis, proofreading") is None


def test_router_messages_includes_recent_history_when_present() -> None:
    without_history = router_messages("you didnt finish?")
    assert len(without_history) == 2
    assert without_history[1]["content"] == "Request to classify:\nyou didnt finish?"

    history = [
        {"role": "user", "content": f"old turn {i}"} for i in range(ROUTER_HISTORY_MAX_MESSAGES + 2)
    ]
    history[-1]["content"] = "x" * (ROUTER_HISTORY_MESSAGE_CHARS + 50)
    with_history = router_messages("you didnt finish?", history=history)
    user_msg = with_history[1]["content"]
    assert "Recent conversation (for context only):" in user_msg
    assert "old turn 0" not in user_msg
    assert "old turn 1" not in user_msg
    assert "old turn 2" in user_msg
    assert ("x" * ROUTER_HISTORY_MESSAGE_CHARS) + "..." in user_msg
    assert user_msg.endswith("Request to classify:\nyou didnt finish?")
