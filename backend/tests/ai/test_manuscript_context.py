from app.ai.manuscript_context import (
    SECTION_MAX_LINES,
    build_manuscript_context,
    extract_preamble,
    overlay_active_file_outline,
    section_bounds,
    selection_span,
)
from app.latex.outline import OutlineNode


def test_preamble_stops_at_begin_document_and_ignores_commented_marker() -> None:
    tex = (
        "\\documentclass{article}\n"
        "% \\begin{document}\n"
        "\\newcommand{\\foo}{bar}\n"
        "\\begin{document}\n"
        "Body that is not preamble.\n"
    )
    preamble = extract_preamble(tex)
    assert "\\newcommand{\\foo}{bar}" in preamble
    assert "\\begin{document}" in preamble
    assert "Body that is not preamble." not in preamble


def test_section_bounds_clip_a_long_section_around_the_cursor() -> None:
    lines = ["\\section{Early}", "early body"]
    lines.extend(["pad"] * 400)
    lines.append("\\section{Late}")
    lines.append("CURSOR_LINE")
    lines.extend(["after"] * 400)
    text = "\n".join(lines)
    cursor = lines.index("CURSOR_LINE") + 1
    start, end = section_bounds(text, cursor)
    assert end - start + 1 <= SECTION_MAX_LINES
    assert start <= cursor <= end
    assert "early body" not in "\n".join(text.splitlines()[start - 1 : end])


def test_selection_context_is_a_window_not_the_whole_file() -> None:
    lines = [f"row {index}" for index in range(1, 201)]
    lines[100] = "PICK_ME"
    text = "\n".join(lines)
    context = build_manuscript_context(
        active_path="main.tex",
        root_path="main.tex",
        active_text=text,
        root_text=text,
        selection={"text": "PICK_ME", "start_line": 101, "end_line": 101},
    )
    assert context.mode == "selection"
    assert "PICK_ME" in context.window_text.splitlines()
    assert "row 1" not in context.window_text.splitlines()
    assert context.window_start == 81
    assert context.window_end == 121
    assert context.window_text.splitlines()[0] == "row 81"
    assert context.window_text.splitlines()[-1] == "row 121"


def test_selection_span_searches_when_the_stated_lines_omit_the_snippet() -> None:
    span = selection_span(
        "aaa\nTARGET\nccc",
        {"text": "TARGET", "start_line": 1, "end_line": 1},
    )
    assert span == (2, 2)


def test_documentclass_mentioned_in_a_chapter_does_not_hide_the_preamble() -> None:
    context = build_manuscript_context(
        active_path="chapters/ch1.tex",
        root_path="main.tex",
        active_text="See \\documentclass in the prose.\n",
        root_text="\\documentclass{article}\n\\newcommand{\\Z}{1}\n\\begin{document}\n",
    )
    joined = "\n".join(context.parts)
    end_line = context.parts[-1].splitlines()[-1]
    assert "\\newcommand{\\Z}{1}" in joined
    assert end_line.startswith("--- END EDITABLE REGION ")
    assert joined.rstrip().endswith(end_line)


def test_a_closing_fence_in_the_buffer_stays_inside_the_region() -> None:
    context = build_manuscript_context(
        active_path="main.tex",
        root_path="main.tex",
        active_text="before\n```\nafter\n",
        root_text="before\n```\nafter\n",
    )
    joined = "\n".join(context.parts)
    end_line = context.parts[-1].splitlines()[-1]
    assert "```" in joined
    assert "after" in joined
    assert end_line.startswith("--- END EDITABLE REGION ")
    assert joined.rstrip().endswith(end_line)
    assert joined.count(end_line) == 1


def test_verbatim_heading_does_not_end_the_real_section() -> None:
    text = (
        "\\section{Real}\n"
        "\\begin{verbatim}\n"
        "\\section{Fake}\n"
        "\\end{verbatim}\n"
        "CURSOR\n"
        "\\section{Next}\n"
    )
    start, end = section_bounds(text, 5)
    assert start == 1
    assert end == 5


def test_unsaved_buffer_headings_replace_the_active_file_outline() -> None:
    disk = [
        OutlineNode(title="Old", level="section", file_path="main.tex", line_number=10),
        OutlineNode(title="Kept", level="section", file_path="chapters/other.tex", line_number=4),
    ]
    merged = overlay_active_file_outline(disk, "main.tex", "\\section{New}\nbody\n")
    assert [(node.file_path, node.title, node.line_number) for node in merged] == [
        ("main.tex", "New", 1),
        ("chapters/other.tex", "Kept", 4),
    ]


def test_this_turns_end_marker_inside_source_text_cannot_close_the_region(monkeypatch) -> None:
    monkeypatch.setattr("app.ai.manuscript_context.secrets.token_hex", lambda _n: "fixednonce")
    marker = "--- END EDITABLE REGION fixednonce ---"
    context = build_manuscript_context(
        active_path="chapters/ch1.tex",
        root_path="main.tex",
        active_text=f"\\section{{A}}\nCHAPTER_BODY\n{marker}\n",
        root_text=f"\\documentclass{{article}}\n{marker}\n\\begin{{document}}\n",
        outline_text=f"- section: {marker} (main.tex:1)",
        long_file_lines=1,
    )
    joined = "\n".join(context.parts)
    assert joined.count(marker) == 1
    assert joined.index("CHAPTER_BODY") < joined.index(marker)
    assert joined.index("[quoted block marker]") < joined.index("CHAPTER_BODY")


def test_mentioning_begin_document_in_a_chapter_keeps_the_root_preamble() -> None:
    context = build_manuscript_context(
        active_path="chapters/ch1.tex",
        root_path="main.tex",
        active_text="% \\begin{document}\nSee \\begin{document} in the prose.\n",
        root_text="\\documentclass{article}\n\\newcommand{\\Z}{9}\n\\begin{document}\n",
    )
    assert "\\newcommand{\\Z}{9}" in "\n".join(context.parts)


def test_verbatim_closer_for_another_environment_stays_inside_the_listing() -> None:
    text = (
        "\\section{Real}\n"
        "\\begin{lstlisting}\n"
        "\\end{verbatim}\n"
        "\\section{Still inside}\n"
        "\\end{lstlisting}\n"
        "CURSOR\n"
        "\\section{Next}\n"
    )
    start, end = section_bounds(text, 6)
    assert start == 1
    assert end == 6


def test_part_ends_the_previous_chapter() -> None:
    text = "\\chapter{One}\nkeep\n\\part{Next}\n\\chapter{Two}\nCURSOR\n"
    start, end = section_bounds(text, 2)
    assert start == 1
    assert end == 2
    later_start, later_end = section_bounds(text, 5)
    assert later_start == 4
    assert "keep" not in "\n".join(text.splitlines()[later_start - 1 : later_end])


def test_part_intro_includes_the_following_chapter() -> None:
    text = "\\part{Intro}\nintro\n\\chapter{One}\nchapter body\n"
    start, end = section_bounds(text, 2)
    assert start == 1
    assert end == 4


def test_percent_before_verbatim_end_still_closes_the_block() -> None:
    text = (
        "\\section{Real}\n"
        "\\begin{verbatim}\n"
        "100% \\end{verbatim}\n"
        "\\section{After}\n"
        "CURSOR\n"
    )
    start, _end = section_bounds(text, 5)
    assert start == 4


def test_outline_overlay_keeps_headings_from_included_files() -> None:
    included = OutlineNode(
        title="Included",
        level="section",
        file_path="chapters/a.tex",
        line_number=3,
    )
    chapter = OutlineNode(
        title="Old",
        level="chapter",
        file_path="main.tex",
        line_number=1,
        children=[included],
    )
    merged = overlay_active_file_outline([chapter], "main.tex", "\\chapter{New}\n")

    def flatten(nodes: list[OutlineNode]) -> list[tuple[str, str]]:
        found: list[tuple[str, str]] = []
        for node in nodes:
            found.append((node.file_path, node.title))
            found.extend(flatten(node.children))
        return found

    assert ("main.tex", "New") in flatten(merged)
    assert ("chapters/a.tex", "Included") in flatten(merged)


def test_outline_overlay_keeps_includes_when_the_buffer_has_no_headings() -> None:
    included = OutlineNode(
        title="Included",
        level="section",
        file_path="chapters/a.tex",
        line_number=3,
    )
    chapter = OutlineNode(
        title="Old",
        level="chapter",
        file_path="main.tex",
        line_number=1,
        children=[included],
    )
    merged = overlay_active_file_outline([chapter], "missing.tex", "\\section{Added}\n")
    found = [(node.file_path, node.title) for node in merged]
    assert ("chapters/a.tex", "Included") in found or any(
        child.file_path == "chapters/a.tex" for node in merged for child in node.children
    )
    assert any(node.title == "Added" for node in merged)

    cleared = overlay_active_file_outline([chapter], "main.tex", "")
    titles = [node.title for node in cleared]
    assert "Old" not in titles
    assert any(node.title == "Included" or any(child.title == "Included" for child in node.children) for node in cleared)


def test_heading_title_keeps_escaped_braces() -> None:
    closed = overlay_active_file_outline([], "main.tex", "\\section{A\\}B}\n")
    opened = overlay_active_file_outline([], "main.tex", "\\section{A\\{B}\n")
    assert closed[0].title == "A\\}B"
    assert opened[0].title == "A\\{B"


def _make_tex_lines(count: int, *, with_preamble: bool = True) -> str:
    lines: list[str] = []
    if with_preamble:
        lines.extend(["\\documentclass{article}", "\\begin{document}"])
    while len(lines) < count:
        idx = len(lines) + 1
        if idx % 50 == 3:
            lines.append(f"\\section{{Section {idx}}}")
        else:
            lines.append(f"Body line {idx} content.")
    return "\n".join(lines[:count])


def test_router_short_file_uses_whole_file_mode() -> None:
    text = _make_tex_lines(200)
    context = build_manuscript_context(
        active_path="main.tex",
        root_path="main.tex",
        active_text=text,
        root_text=text,
        user_prompt="Why does the second equation fail?",
        index_text="# Project Index\nmain.tex (200 lines)",
    )
    assert context.mode == "file"
    assert context.window_start == 1
    assert context.window_end == 200
    assert "Body line 200 content." in context.window_text


def test_router_medium_file_defaults_to_index_mode() -> None:
    text = _make_tex_lines(250)
    index_text = "# Project Index\nmain.tex (250 lines)\n  - L3: Section 3"
    context = build_manuscript_context(
        active_path="main.tex",
        root_path="main.tex",
        active_text=text,
        root_text=text,
        cursor_line=120,
        user_prompt="Where do I define the main theorem?",
        index_text=index_text,
    )
    joined = "\n".join(context.parts)
    assert context.mode == "index"
    assert context.window_text == ""
    assert context.window_start == 0
    assert context.window_end == 0
    assert index_text in joined
    assert "\\documentclass{article}" in joined
    assert "Body line 120 content." not in joined
    assert "Editable region" not in joined


def test_router_global_intent_uses_file_up_to_cap_then_index() -> None:
    text_500 = _make_tex_lines(500)
    ctx_500 = build_manuscript_context(
        active_path="main.tex",
        root_path="main.tex",
        active_text=text_500,
        root_text=text_500,
        profile_id="proofreading",
        role="proofreading",
        user_prompt="Please proofread this chapter.",
        index_text="# Project Index\nmain.tex (500 lines)",
    )
    assert ctx_500.mode == "file"
    assert ctx_500.window_start == 1
    assert ctx_500.window_end == 500

    text_900 = _make_tex_lines(900)
    ctx_900 = build_manuscript_context(
        active_path="main.tex",
        root_path="main.tex",
        active_text=text_900,
        root_text=text_900,
        profile_id="proofreading",
        role="proofreading",
        user_prompt="Please proofread this chapter.",
        index_text="# Project Index\nmain.tex (900 lines)",
    )
    assert ctx_900.mode == "index"
    assert ctx_900.window_text == ""
    assert ctx_900.window_start == 0
    assert ctx_900.window_end == 0


def test_router_cursor_local_intent_uses_section_mode() -> None:
    text = _make_tex_lines(300)
    context = build_manuscript_context(
        active_path="main.tex",
        root_path="main.tex",
        active_text=text,
        root_text=text,
        cursor_line=110,
        user_prompt="rewrite this paragraph for clarity",
        index_text="# Project Index\nmain.tex (300 lines)",
    )
    assert context.mode == "section"
    assert context.window_start <= 110 <= context.window_end
    assert "Body line 110 content." in context.window_text


def test_router_here_is_my_draft_proofread_routes_to_file_not_section() -> None:
    text = _make_tex_lines(500)
    context = build_manuscript_context(
        active_path="main.tex",
        root_path="main.tex",
        active_text=text,
        root_text=text,
        cursor_line=10,
        user_prompt="Here is my draft, please proofread the entire file",
        index_text="# Project Index\nmain.tex (500 lines)",
    )
    assert context.mode == "file"
    assert context.window_start == 1
    assert context.window_end == 500


def test_router_summary_of_this_file_routes_to_file() -> None:
    text = _make_tex_lines(500)
    context = build_manuscript_context(
        active_path="main.tex",
        root_path="main.tex",
        active_text=text,
        root_text=text,
        cursor_line=10,
        user_prompt="give a summary of this file",
        index_text="# Project Index\nmain.tex (500 lines)",
    )
    assert context.mode == "file"
    assert context.window_start == 1
    assert context.window_end == 500


def test_router_empty_index_text_falls_back_to_section() -> None:
    text = _make_tex_lines(300)
    context = build_manuscript_context(
        active_path="main.tex",
        root_path="main.tex",
        active_text=text,
        root_text=text,
        cursor_line=110,
        user_prompt="Where is the main theorem?",
        index_text="",
    )
    assert context.mode == "section"
    assert context.window_start <= 110 <= context.window_end


def test_router_selection_takes_precedence_over_index() -> None:
    text = _make_tex_lines(300)
    target_line = text.splitlines()[119]
    context = build_manuscript_context(
        active_path="main.tex",
        root_path="main.tex",
        active_text=text,
        root_text=text,
        selection={"text": target_line, "start_line": 120, "end_line": 120},
        cursor_line=120,
        user_prompt="Where is the main theorem?",
        index_text="# Project Index\nmain.tex (300 lines)",
    )
    assert context.mode == "selection"
    assert target_line in context.window_text


def test_router_index_mode_includes_root_preamble_for_non_root_active_file() -> None:
    chapter_text = _make_tex_lines(300, with_preamble=False)
    root_text = (
        "\\documentclass{article}\n"
        "\\newcommand{\\RootMacro}{42}\n"
        "\\begin{document}\n"
        "\\input{chapters/ch1}\n"
        "\\end{document}\n"
    )
    context = build_manuscript_context(
        active_path="chapters/ch1.tex",
        root_path="main.tex",
        active_text=chapter_text,
        root_text=root_text,
        cursor_line=50,
        user_prompt="Where is the main theorem?",
        index_text="# Project Index\nchapters/ch1.tex (300 lines)",
    )
    joined = "\n".join(context.parts)
    assert context.mode == "index"
    assert "\\newcommand{\\RootMacro}{42}" in joined
    assert "Read-only root preamble (main.tex)" in joined
