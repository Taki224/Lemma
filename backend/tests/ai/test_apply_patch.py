from __future__ import annotations

import pytest

from app.ai.apply_patch import (
    ApplyPatchError,
    apply_v4a_update,
    changed_line_spans,
    paths_refer_to_same_file,
)


def test_apply_openrouter_update_example() -> None:
    original = 'function main() {\n  console.log("old");\n}\n'
    diff = '@@ function main() {\n-  console.log("old");\n+  console.log("new");\n }\n'
    assert apply_v4a_update(original, diff) == (
        'function main() {\n  console.log("new");\n}\n'
    )


def test_apply_several_hunks_and_keeps_trailing_newline() -> None:
    original = "function main() {\n  return 1;\n}\n\nfunction other() {\n  return 1;\n}\n"
    diff = (
        "@@ function main() {\n"
        "-  return 1;\n"
        "+  return 2;\n"
        " }\n"
        "@@ function other() {\n"
        "-  return 1;\n"
        "+  return 3;\n"
        " }\n"
    )
    updated = apply_v4a_update(original, diff)
    assert "return 2;" in updated
    assert "return 3;" in updated
    assert updated.endswith("\n")
    spans = changed_line_spans(original, updated)
    assert [(span[0], span[2], span[3]) for span in spans] == [
        (2, "  return 1;", "  return 2;"),
        (6, "  return 1;", "  return 3;"),
    ]


def test_apply_end_of_file_insertion_borrows_the_last_line() -> None:
    original = "alpha\nomega\n"
    diff = "@@\n+beta\n*** End of File\n"
    updated = apply_v4a_update(original, diff)
    assert updated == "alpha\nomega\nbeta\n"
    assert changed_line_spans(original, updated) == [(2, 2, "omega", "omega\nbeta")]


def test_apply_wrapped_patch() -> None:
    original = "alpha\nomega\n"
    diff = (
        "*** Begin Patch\n"
        "*** Update File: main.tex\n"
        "@@ alpha\n"
        "-omega\n"
        "+omega!\n"
        "*** End Patch\n"
    )
    assert apply_v4a_update(original, diff) == "alpha\nomega!\n"


def test_apply_matches_trailing_whitespace_before_giving_up() -> None:
    original = "alpha\nomega \n"
    diff = "@@ alpha\n-omega\n+omega!\n"
    assert apply_v4a_update(original, diff) == "alpha\nomega!\n"


def test_apply_rejects_a_missing_context_and_a_rename() -> None:
    with pytest.raises(ApplyPatchError, match="could not find"):
        apply_v4a_update("alpha\n", "@@ missing\n-alpha\n+beta\n")
    with pytest.raises(ApplyPatchError, match="renames"):
        apply_v4a_update(
            "alpha\n",
            "*** Begin Patch\n*** Update File: a.tex\n*** Move to: b.tex\n@@\n-alpha\n+beta\n*** End Patch\n",
        )


def test_paths_accept_a_prefix_but_not_a_shorter_name() -> None:
    assert paths_refer_to_same_file("main.tex", "/workspace/main.tex")
    assert paths_refer_to_same_file("chapters/main.tex", "chapters/main.tex")
    assert not paths_refer_to_same_file("chapters/main.tex", "main.tex")


def test_insertion_at_the_start_borrows_the_following_line() -> None:
    original = "alpha\nomega\n"
    diff = "@@\n+intro\n"
    updated = apply_v4a_update(original, diff)
    assert updated == "intro\nalpha\nomega\n"
    assert changed_line_spans(original, updated) == [(1, 1, "alpha", "intro\nalpha")]
