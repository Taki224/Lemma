from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

DiagnosticSeverity = Literal["error", "warning", "typesetting"]

_FILE_LINE_ERROR_RE = re.compile(
    r"^(?P<file>(?:\./|/)?[A-Za-z0-9_./-]+\.(?:tex|cls|sty|bib|bbl)):(?P<line>\d+):\s*(?P<msg>.+)$"
)
_BANG_ERROR_RE = re.compile(r"^!\s*(?P<msg>.+)$")
_MISSING_FILE_RE = re.compile(
    r"File\s+[`'](?P<pkg>[A-Za-z0-9_.-]+\.(?:sty|cls|bst|def|cfg))['`]\s+not found"
)
_LINE_INDICATOR_RE = re.compile(r"^l\.(?P<line>\d+)\s*(?P<snippet>.*)$")
_ABORT_REASON_RE = re.compile(r"^\*\*\*\s*\((?P<reason>[^)]+)\)")
_LATEX_WARNING_RE = re.compile(
    r"^(?:LaTeX|Package\s+[A-Za-z0-9_.-]+)\s+Warning:\s*(?P<msg>.+)$"
)
_WARNING_LINE_RE = re.compile(r"on input line\s+(?P<line>\d+)")
_BOX_WARNING_RE = re.compile(
    r"^(?P<msg>(?:Overfull|Underfull)\s+\\[hv]box\s+[^\n]+)"
)
_BOX_LINES_RE = re.compile(r"at lines?\s+(?P<line>\d+)(?:--\d+)?")
_BIBTEX_LOCATION_RE = re.compile(
    r"---(?:line (?P<line>\d+) of|while reading) file (?P<file>\S+)\s*$"
)
_BIBER_ERROR_RE = re.compile(r"^(?:\[\d+\]\s+\S+>\s+)?ERROR - (?P<msg>.+)$")


@dataclass(frozen=True)
class LatexDiagnostic:
    severity: DiagnosticSeverity
    file_path: str
    line: int | None
    message: str
    context_lines: str
    missing_package: str | None = None


def _clean_rel_file(raw_path: str, root_tex: str) -> str:
    cleaned = raw_path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned or root_tex


def parse_latex_log(log_text: str, root_tex: str = "main.tex") -> list[LatexDiagnostic]:
    """Parse raw pdflatex / latexmk log output into structured line-linked diagnostics."""
    lines = log_text.splitlines()
    diagnostics: list[LatexDiagnostic] = []
    n = len(lines)
    idx = 0

    pending_missing_pkg: str | None = None

    while idx < n:
        raw_line = lines[idx].strip()

        # 1. Check for ! LaTeX Error: File `foo.sty' not found or any general ! Error block
        bang_match = _BANG_ERROR_RE.match(raw_line)
        if bang_match:
            msg = bang_match.group("msg").strip()
            pkg_match = _MISSING_FILE_RE.search(msg)
            found_pkg = pkg_match.group("pkg") if pkg_match else pending_missing_pkg
            pending_missing_pkg = None

            found_line: int | None = None
            found_file = root_tex
            context_buf: list[str] = [raw_line]
            look = idx + 1
            while look < min(n, idx + 12):
                ahead = lines[look].strip()
                context_buf.append(ahead)
                fle = _FILE_LINE_ERROR_RE.match(ahead)
                if fle:
                    found_file = _clean_rel_file(fle.group("file"), root_tex)
                    found_line = int(fle.group("line"))
                abort = _ABORT_REASON_RE.match(ahead)
                if abort and msg == "Emergency stop.":
                    msg = f"Emergency stop: {abort.group('reason').strip()}"
                    break
                li = _LINE_INDICATOR_RE.match(ahead)
                if li:
                    if found_line is None:
                        found_line = int(li.group("line"))
                    idx = look
                    break
                look += 1

            diagnostics.append(
                LatexDiagnostic(
                    severity="error",
                    file_path=found_file,
                    line=found_line,
                    message=msg,
                    context_lines="\n".join(c for c in context_buf if c),
                    missing_package=found_pkg,
                )
            )
            idx += 1
            continue

        # 2. Check for -file-line-error entries: ./chapters/intro.tex:42: Undefined control sequence.
        fle_match = _FILE_LINE_ERROR_RE.match(raw_line)
        if fle_match:
            file_path = _clean_rel_file(fle_match.group("file"), root_tex)
            line_num = int(fle_match.group("line"))
            msg = fle_match.group("msg").strip()

            pkg_match = _MISSING_FILE_RE.search(msg)
            missing_pkg = pkg_match.group("pkg") if pkg_match else pending_missing_pkg
            pending_missing_pkg = None

            context_parts: list[str] = [raw_line]
            look = idx + 1
            while look < min(n, idx + 6):
                ahead_raw = lines[look]
                ahead_stripped = ahead_raw.strip()
                if (
                    _FILE_LINE_ERROR_RE.match(ahead_stripped)
                    or _BANG_ERROR_RE.match(ahead_stripped)
                    or _LATEX_WARNING_RE.match(ahead_stripped)
                    or _BOX_WARNING_RE.match(ahead_stripped)
                ):
                    break
                context_parts.append(ahead_raw.rstrip())
                if _LINE_INDICATOR_RE.match(ahead_stripped):
                    # Also include the continuation line right after l.NN if indented
                    if look + 1 < n and lines[look + 1].startswith(" "):
                        context_parts.append(lines[look + 1].rstrip())
                        look += 1
                    idx = look
                    break
                look += 1

            diagnostics.append(
                LatexDiagnostic(
                    severity="error",
                    file_path=file_path,
                    line=line_num,
                    message=msg,
                    context_lines="\n".join(context_parts),
                    missing_package=missing_pkg,
                )
            )
            idx += 1
            continue

        # 3. Check for LaTeX / Package Warning
        warn_match = _LATEX_WARNING_RE.match(raw_line)
        if warn_match:
            msg = warn_match.group("msg").strip()
            # epstopdf always prints this because compiles run with shell escape off on purpose.
            if msg == "Shell escape feature is not enabled.":
                idx += 1
                continue
            line_match = _WARNING_LINE_RE.search(msg)
            warn_line = int(line_match.group("line")) if line_match else None
            warn_file = root_tex
            if "Empty `thebibliography' environment" in msg or "Empty 'thebibliography' environment" in msg:
                warn_file = "references.bib"
                warn_line = None
            diagnostics.append(
                LatexDiagnostic(
                    severity="warning",
                    file_path=warn_file,
                    line=warn_line,
                    message=f"LaTeX Warning: {msg}",
                    context_lines=raw_line,
                    missing_package=None,
                )
            )
            idx += 1
            continue

        # 4. Check for Overfull / Underfull \hbox or \vbox
        box_match = _BOX_WARNING_RE.match(raw_line)
        if box_match:
            msg = box_match.group("msg").strip()
            box_line_match = _BOX_LINES_RE.search(msg)
            box_line = int(box_line_match.group("line")) if box_line_match else None
            context = raw_line
            if idx + 1 < n and lines[idx + 1].startswith("[]"):
                context = f"{raw_line}\n{lines[idx + 1].strip()}"
                idx += 1
            diagnostics.append(
                LatexDiagnostic(
                    severity="typesetting",
                    file_path=root_tex,
                    line=box_line,
                    message=msg,
                    context_lines=context,
                    missing_package=None,
                )
            )
            idx += 1
            continue

        idx += 1

    return diagnostics


def parse_bibliography_log(blg_text: str, root_tex: str = "main.tex") -> list[LatexDiagnostic]:
    """Parse BibTeX or Biber .blg output into error diagnostics."""
    lines = [line.rstrip() for line in blg_text.splitlines()]
    diagnostics: list[LatexDiagnostic] = []
    for idx, raw_line in enumerate(lines):
        biber_match = _BIBER_ERROR_RE.match(raw_line)
        if biber_match:
            diagnostics.append(
                LatexDiagnostic(
                    severity="error",
                    file_path=root_tex,
                    line=None,
                    message=f"Biber: {biber_match.group('msg').strip()}",
                    context_lines=raw_line,
                )
            )
            continue

        loc_match = _BIBTEX_LOCATION_RE.search(raw_line)
        if not loc_match:
            continue
        msg = raw_line[: loc_match.start()].strip()
        context = [raw_line]
        if not msg and idx > 0:
            msg = lines[idx - 1].strip()
            context.insert(0, lines[idx - 1])
        loc_file = _clean_rel_file(loc_match.group("file"), root_tex)
        on_aux = loc_file.endswith(".aux")
        loc_line = loc_match.group("line")
        diagnostics.append(
            LatexDiagnostic(
                severity="error",
                file_path=root_tex if on_aux else loc_file,
                line=int(loc_line) if loc_line and not on_aux else None,
                message=f"BibTeX: {msg}",
                context_lines="\n".join(context),
            )
        )
    return diagnostics
