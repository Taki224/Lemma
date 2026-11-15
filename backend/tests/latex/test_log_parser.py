from __future__ import annotations

import pytest

from app.latex.log_parser import LatexDiagnostic, parse_latex_log


def test_parse_file_line_errors_and_undefined_control_sequence() -> None:
    sample_log = (
        "This is pdfTeX, Version 3.141592653-2.6-1.40.26 (TeX Live 2024)\n"
        "(./main.tex\n"
        "(./chapters/intro.tex\n"
        "./chapters/intro.tex:42: Undefined control sequence.\n"
        "l.42 We invoke \\nonexistentmacro\n"
        "                                {arg} here.\n"
        "./chapters/intro.tex:58: Missing $ inserted.\n"
        "<inserted text> \n"
        "                $\n"
        "l.58 The energy is E = mc^2\n"
        "Overfull \\hbox (14.22636pt too wide) in paragraph at lines 60--64\n"
        "[]\\OT1/cmr/m/n/10 A very long unbreakable token sequence...\n"
        "))\n"
    )

    diagnostics = parse_latex_log(log_text=sample_log, root_tex="main.tex")
    assert len(diagnostics) == 3

    err1: LatexDiagnostic = diagnostics[0]
    assert err1.severity == "error"
    assert err1.file_path == "chapters/intro.tex"
    assert err1.line == 42
    assert "Undefined control sequence" in err1.message
    assert "\\nonexistentmacro" in err1.context_lines
    assert err1.missing_package is None

    err2: LatexDiagnostic = diagnostics[1]
    assert err2.severity == "error"
    assert err2.file_path == "chapters/intro.tex"
    assert err2.line == 58
    assert "Missing $ inserted" in err2.message

    box_warn: LatexDiagnostic = diagnostics[2]
    assert box_warn.severity == "typesetting"
    assert box_warn.line == 60
    assert "Overfull \\hbox" in box_warn.message


def test_parse_missing_package_and_undefined_citation_warnings() -> None:
    sample_log = (
        "(./main.tex\n"
        "! LaTeX Error: File `cleveref.sty' not found.\n"
        "\n"
        "Type X to quit or <RETURN> to proceed,\n"
        "or enter new name. (Default extension: sty)\n"
        "\n"
        "Enter file name: \n"
        "./main.tex:9: Emergency stop.\n"
        "<read *> \n"
        "l.9 \\usepackage{cleveref}\n"
        "LaTeX Warning: Citation `vaswani2099future' on page 2 undefined on input line 31.\n"
        "LaTeX Warning: Reference `fig:missing' on page 3 undefined on input line 45.\n"
        "Underfull \\vbox (badness 10000) has occurred while \\output is active [4]\n"
        ")\n"
    )

    diagnostics = parse_latex_log(log_text=sample_log, root_tex="main.tex")
    errors = [d for d in diagnostics if d.severity == "error"]
    warnings = [d for d in diagnostics if d.severity == "warning"]
    typesetting = [d for d in diagnostics if d.severity == "typesetting"]

    assert any(d.missing_package == "cleveref.sty" for d in errors)
    pkg_diag = next(d for d in errors if d.missing_package == "cleveref.sty")
    assert pkg_diag.file_path == "main.tex"
    assert pkg_diag.line == 9

    assert len(warnings) == 2
    assert "vaswani2099future" in warnings[0].message
    assert warnings[0].line == 31
    assert "fig:missing" in warnings[1].message
    assert warnings[1].line == 45

    assert len(typesetting) == 1
    assert "Underfull \\vbox" in typesetting[0].message


@pytest.mark.parametrize(
    "warn_str",
    [
        "LaTeX Warning: Empty `thebibliography' environment on input line 3.",
        "LaTeX Warning: Empty 'thebibliography' environment on input line 3.",
    ],
)
def test_empty_thebibliography_attributed_to_bib(warn_str: str) -> None:
    sample_log = (
        "(./main.tex\n"
        "(./main.bbl\n"
        f"{warn_str}\n"
        ")\n"
        ")\n"
    )
    diagnostics = parse_latex_log(log_text=sample_log, root_tex="main.tex")
    assert len(diagnostics) == 1
    diag = diagnostics[0]
    assert diag.severity == "warning"
    assert diag.file_path == "references.bib"
    assert diag.line is None



def test_intentional_shell_escape_warning_is_hidden() -> None:
    sample_log = (
        "Package epstopdf Warning: Shell escape feature is not enabled.\n"
        "LaTeX Warning: Citation `yao_react_2023' on page 1 undefined on input line 52.\n"
    )
    diagnostics = parse_latex_log(log_text=sample_log, root_tex="manuscript.tex")
    assert [d.message for d in diagnostics] == [
        "LaTeX Warning: Citation `yao_react_2023' on page 1 undefined on input line 52."
    ]


def test_emergency_stop_includes_abort_reason() -> None:
    sample_log = (
        "(./manuscript.tex\n"
        "! Emergency stop.\n"
        "<*> manuscript.tex\n"
        "*** (job aborted, no legal \\end found)\n"
        "\n"
        "!  ==> Fatal error occurred, no output PDF file produced!\n"
    )
    diagnostics = parse_latex_log(log_text=sample_log, root_tex="manuscript.tex")
    assert diagnostics[0].severity == "error"
    assert diagnostics[0].message == "Emergency stop: job aborted, no legal \\end found"


def test_parse_bibliography_log_reports_bibtex_and_biber_errors() -> None:
    from app.latex.log_parser import parse_bibliography_log

    sample_blg = (
        "The top-level auxiliary file: manuscript.aux\n"
        "I couldn't open style file IEEEtranS.bst\n"
        "---line 3 of file manuscript.aux\n"
        " : \\bibstyle{IEEEtranS\n"
        "I found no style file---while reading file manuscript.aux\n"
        "I was expecting a `,' or a `}'---line 12 of file references.bib\n"
        "[42] Utils.pm:209> ERROR - Cannot find 'references.bib'!\n"
        "(There were 3 error messages)\n"
    )
    diagnostics = parse_bibliography_log(blg_text=sample_blg, root_tex="manuscript.tex")
    assert [(d.file_path, d.line, d.message) for d in diagnostics] == [
        ("manuscript.tex", None, "BibTeX: I couldn't open style file IEEEtranS.bst"),
        ("manuscript.tex", None, "BibTeX: I found no style file"),
        ("references.bib", 12, "BibTeX: I was expecting a `,' or a `}'"),
        ("manuscript.tex", None, "Biber: Cannot find 'references.bib'!"),
    ]
    assert all(d.severity == "error" for d in diagnostics)
