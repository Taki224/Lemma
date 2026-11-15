from __future__ import annotations

from pathlib import Path

from app.latex.outline import parse_project_outline


def test_outline_resolves_nested_input_and_include_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "manuscript"
    project_dir.mkdir()
    (project_dir / "chapters").mkdir()

    (project_dir / "main.tex").write_text(
        "\\documentclass{report}\n"
        "\\begin{document}\n"
        "\\part{Foundations}\n"
        "\\include{chapters/intro}\n"
        "\\input{chapters/methods.tex}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )

    (project_dir / "chapters" / "intro.tex").write_text(
        "% Chapter 1\n"
        "\\chapter{Introduction}\n"
        "\\section{Background: \\textbf{Quantum} Realm}\n"
        "\\subsection{Historical Context}\n",
        encoding="utf-8",
    )

    (project_dir / "chapters" / "methods.tex").write_text(
        "\\chapter{Methodology}\n"
        "\\section*{Experimental Setup}\n"
        "\\subsubsection{Apparatus}\n",
        encoding="utf-8",
    )

    outline = parse_project_outline(project_dir=project_dir, root_tex="main.tex")
    assert len(outline) == 1
    part = outline[0]
    assert part.title == "Foundations"
    assert part.level == "part"
    assert part.file_path == "main.tex"
    assert part.line_number == 3

    assert len(part.children) == 2
    ch1, ch2 = part.children
    assert ch1.title == "Introduction"
    assert ch1.level == "chapter"
    assert ch1.file_path == "chapters/intro.tex"
    assert ch1.line_number == 2
    assert len(ch1.children) == 1
    sec1 = ch1.children[0]
    assert sec1.title == "Background: \\textbf{Quantum} Realm"
    assert sec1.level == "section"
    assert sec1.file_path == "chapters/intro.tex"
    assert sec1.line_number == 3
    assert len(sec1.children) == 1
    assert sec1.children[0].title == "Historical Context"
    assert sec1.children[0].level == "subsection"
    assert sec1.children[0].line_number == 4

    assert ch2.title == "Methodology"
    assert ch2.level == "chapter"
    assert ch2.file_path == "chapters/methods.tex"
    assert ch2.line_number == 1
    assert len(ch2.children) == 1
    assert ch2.children[0].title == "Experimental Setup"
    assert ch2.children[0].level == "section"


def test_outline_ignores_commented_sections_and_breaks_cycles(tmp_path: Path) -> None:
    project_dir = tmp_path / "cyclic"
    project_dir.mkdir()

    (project_dir / "main.tex").write_text(
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "% \\section{Commented Out Section}\n"
        "\\section{Real Section} % \\subsection{Inline Commented}\n"
        "\\input{a}\n"
        "\\input{../../outside_secret}\n"
        "\\end{document}\n",
        encoding="utf-8",
    )
    (project_dir / "a.tex").write_text(
        "\\subsection{From A}\n"
        "\\input{b}\n",
        encoding="utf-8",
    )
    (project_dir / "b.tex").write_text(
        "\\subsubsection{From B}\n"
        "\\input{a}\n",
        encoding="utf-8",
    )

    outline = parse_project_outline(project_dir=project_dir, root_tex="main.tex")
    assert len(outline) == 1
    sec = outline[0]
    assert sec.title == "Real Section"
    assert sec.level == "section"
    assert len(sec.children) == 1
    sub = sec.children[0]
    assert sub.title == "From A"
    assert sub.file_path == "a.tex"
    assert len(sub.children) == 1
    subsub = sub.children[0]
    assert subsub.title == "From B"
    assert subsub.file_path == "b.tex"
