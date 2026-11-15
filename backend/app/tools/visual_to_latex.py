from __future__ import annotations

import csv
import io
import re
import secrets as pysecrets
from dataclasses import dataclass, field
from typing import Any, Literal

from app.ai.chat_service import ProposedHunk
from app.ai.openrouter import OpenRouterClient
from app.grounding.citation_verifier import BibEntry, verify_citations

TargetFormat = Literal["equation", "booktabs", "tikz"]


@dataclass
class VisualLatexResult:
    latex_code: str
    target_format: str
    hunk: ProposedHunk | None = None
    lint_warnings: list[str] = field(default_factory=list)
    unknown_cite_keys: list[str] = field(default_factory=list)
    is_safe: bool = True


_UNSAFE_PATTERNS = [
    (re.compile(r"(?:\\immediate\s*)?\\write18\b", re.IGNORECASE), "Command \\write18 is forbidden for security."),
    (re.compile(r"\\openout\b", re.IGNORECASE), "Command \\openout is forbidden for security."),
    (re.compile(r"\\(?:input|include)\s*\{[^}]*\.\.[^}]*\}", re.IGNORECASE), "Path traversal in \\input or \\include is forbidden."),
]


def _escape_latex_cell(cell: str) -> str:
    """Escapes %, &, _, #, $, {, } when not already preceded by backslash."""
    escaped = re.sub(r"(?<!\\)([%&_#${}])", r"\\\1", cell.strip())
    return escaped


def convert_csv_to_booktabs(csv_text: str, caption: str = "", label: str = "tab:results") -> str:
    """Deterministic parser converting CSV/TSV spreadsheet data into clean booktabs LaTeX table."""
    cleaned = csv_text.strip()
    if not cleaned:
        return ""

    # Detect delimiter: tab or comma
    dialect = "excel-tab" if "\t" in cleaned and "," not in cleaned else "excel"
    try:
        reader = list(csv.reader(io.StringIO(cleaned), dialect=dialect))
    except Exception:
        reader = list(csv.reader(io.StringIO(cleaned)))

    # Filter out empty rows
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        return ""

    num_cols = max(len(r) for r in rows)
    col_spec = "l" + "c" * (num_cols - 1) if num_cols > 1 else "l"

    lines = [
        r"\begin{table}[htbp]",
        r"\centering",
        f"\\begin{{tabular}}{{{col_spec}}}",
        r"\toprule",
    ]

    # First row as header
    header_row = rows[0]
    escaped_header = [_escape_latex_cell(header_row[i]) if i < len(header_row) else "" for i in range(num_cols)]
    lines.append(" & ".join(escaped_header) + r" \\")
    lines.append(r"\midrule")

    # Remaining rows
    for row in rows[1:]:
        escaped_cells = [_escape_latex_cell(row[i]) if i < len(row) else "" for i in range(num_cols)]
        lines.append(" & ".join(escaped_cells) + r" \\")

    lines.append(r"\bottomrule")
    lines.append(r"\end{tabular}")

    if caption:
        lines.append(f"\\caption{{{_escape_latex_cell(caption)}}}")
    if label:
        clean_label = re.sub(r"[^a-zA-Z0-9:_-]", "", label.strip())
        if clean_label:
            lines.append(f"\\label{{{clean_label}}}")

    lines.append(r"\end{table}")
    return "\n".join(lines)


def lint_generated_latex_snippet(
    latex_code: str,
    bib_entries: dict[str, BibEntry],
    staged_keys: set[str],
    target_format: str = "equation",
    file_path: str = "main.tex",
    base_etag: str = "",
    start_line: int = 1,
    end_line: int = 1,
    original_text: str = "",
) -> VisualLatexResult:
    """Scans generated LaTeX for unsafe commands and verifies citations, returning VisualLatexResult."""
    lint_warnings: list[str] = []
    is_safe = True

    # 1. Check for unsafe commands
    for pattern, msg in _UNSAFE_PATTERNS:
        if pattern.search(latex_code):
            lint_warnings.append(msg)
            is_safe = False

    # 2. Check citations
    verification = verify_citations(latex_code, bib_entries=bib_entries, staged_keys=staged_keys)
    if verification.unknown_keys:
        is_safe = False
        for k in verification.unknown_keys:
            lint_warnings.append(f"Unknown citation key: {k}")

    hunk = ProposedHunk(
        hunk_id=f"hunk_{pysecrets.token_hex(8)}",
        file_path=file_path,
        base_etag=base_etag,
        start_line=start_line,
        end_line=end_line,
        original_text=original_text,
        replacement_text=latex_code,
        cite_keys=verification.valid_keys + verification.staged_keys,
        unknown_cite_keys=verification.unknown_keys,
    )

    return VisualLatexResult(
        latex_code=latex_code,
        target_format=target_format,
        hunk=hunk,
        lint_warnings=lint_warnings,
        unknown_cite_keys=verification.unknown_keys,
        is_safe=is_safe,
    )


def _strip_markdown_fences(content: str) -> str:
    cleaned = content.strip()
    match = re.search(r"```(?:latex|tex)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return cleaned


async def convert_image_to_latex(
    image_bytes: bytes,
    target_format: TargetFormat,
    model: str,
    openrouter: OpenRouterClient,
    bib_entries: dict[str, BibEntry],
    staged_keys: set[str],
    file_path: str = "main.tex",
    base_etag: str = "",
) -> VisualLatexResult:
    """Prompts vision model to convert pasted screenshots into clean LaTeX and passes through linting."""
    format_prompts = {
        "equation": "You are an expert LaTeX assistant. Convert this equation image into clean LaTeX math code. Output ONLY the LaTeX code, wrapped in an equation environment or align environment. Do not include markdown commentary.",
        "booktabs": "You are an expert LaTeX assistant. Convert this table image into clean LaTeX table code using booktabs (toprule, midrule, bottomrule) and centering. Output ONLY the LaTeX table code. Do not include markdown commentary.",
        "tikz": "You are an expert LaTeX assistant. Convert this diagram or sketch into clean LaTeX TikZ code wrapped in \\begin{tikzpicture} ... \\end{tikzpicture}. Output ONLY the LaTeX TikZ code. Do not include markdown commentary.",
    }

    system_prompt = format_prompts.get(target_format, format_prompts["equation"])
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Please convert this image into LaTeX {target_format}."},
    ]

    res = await openrouter.complete(
        model=model,
        messages=messages,
        vision_images=[image_bytes],
        max_tokens=4096,
    )

    extracted_latex = _strip_markdown_fences(res.content)

    result = lint_generated_latex_snippet(
        latex_code=extracted_latex,
        bib_entries=bib_entries,
        staged_keys=staged_keys,
        target_format=target_format,
        file_path=file_path,
        base_etag=base_etag,
    )
    return result
