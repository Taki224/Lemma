"""Tiered manuscript context for chat.

An 80-page manuscript stays accurate when each turn sends a short editable window,
the root preamble when that window does not already include it, and an outline
only when the active file is too long to send whole. SEARCH blocks can only
anchor text that appears in the editable window.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Literal

# Short files fit comfortably in a single turn without index routing.
SHORT_FILE_LINES = 200
# A manuscript chapter fits under this for global intents; above it, route to index.
GLOBAL_FILE_MAX_LINES = 800
# Backward-compatible alias for callers not yet migrated to the new router thresholds.
LONG_FILE_LINES = GLOBAL_FILE_MAX_LINES
# Lines of context on each side of a selection (~40 lines around the passage).
SELECTION_PAD_LINES = 20
# Cap for the cursor section when that section is itself long.
SECTION_MAX_LINES = 160

ContextMode = Literal["selection", "section", "file", "index"]

_NO_EDIT_PROFILE_IDS = frozenset({"verify_claim", "explain_math", "explain"})
_EDIT_ROLES = frozenset(
    {"writing", "synthesis", "proofreading", "quick_action", "compile_autofix"}
)

_COMMENT_RE = re.compile(r"(?<!\\)%.*$")
_HEADING_RE = re.compile(
    r"\\(?P<cmd>subsubsection|subsection|section|chapter|part)\*?(?:\[[^\]]*\])?\{"
)
_OUTLINE_HEADING_RE = _HEADING_RE
_VERBATIM_BEGIN_RE = re.compile(
    r"\\begin\{(?P<env>verbatim\*?|lstlisting|minted)\}"
)
_VERBATIM_END_RE = re.compile(
    r"\\end\{(?P<env>verbatim\*?|lstlisting|minted)\}"
)
_HEADING_RANK = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
}
_MARKER_PLACEHOLDER = "[quoted block marker]"

SEARCH_CONTRACT = (
    "When proposing manuscript edits, use SEARCH/REPLACE blocks. "
    "Copy SEARCH verbatim from the editable region. "
    "Do not return the whole file and do not wrap the manuscript in a latex code fence.\n\n"
    "<<<<<<< SEARCH\n"
    "verbatim lines from the editable region\n"
    "=======\n"
    "replacement lines\n"
    ">>>>>>> REPLACE\n\n"
    "Leave any read-only preamble unchanged."
)


@dataclass(frozen=True)
class ManuscriptContext:
    mode: ContextMode
    window_text: str
    window_start: int
    window_end: int
    parts: list[str]
    quote_markers: tuple[str, ...] = ()


_CURSOR_LOCAL_RE = re.compile(
    r"\b(?:"
    r"at\s+(?:the\s+)?cursor"
    r"|this\s+(?:paragraph|sentence|line)"
    r"|right\s+here"
    r"|at\s+this\s+point"
    r"|around\s+line"
    r")\b",
    re.IGNORECASE,
)

_GLOBAL_INTENT_RE = re.compile(
    r"\b(?:"
    r"proofread(?:ing)?"
    r"|summari[sz]e"
    r"|summary"
    r"|entire\s+(?:file|document|manuscript|paper|thesis|chapter)"
    r"|whole\s+(?:file|document|manuscript|paper|thesis|chapter)"
    r"|review\s+the\s+whole"
    r"|for\s+every\b"
    r"|every\s+(?:referenced|linked|included|cited|file|path|link|image|figure|occurrence|instance)\b"
    r"|everywhere"
    r"|throughout\s+the\s+(?:file|document|manuscript|paper|thesis|chapter)"
    r"|across\s+the\s+(?:file|document|manuscript|paper|thesis|chapter)"
    r"|(?:update|replace|change|fix|check|find|rename|move)\s+all\b"
    r"|all\s+(?:occurrences|instances|paths|links|references|files|images|figures|citations)\b"
    r")\b",
    re.IGNORECASE,
)


def is_cursor_local_intent(prompt: str) -> bool:
    """Return True when the prompt explicitly refers to the cursor location."""
    return bool(_CURSOR_LOCAL_RE.search(prompt or ""))


def is_global_intent(prompt: str, profile_id: str, role: str) -> bool:
    """Return True for proofreading or prompts that ask to inspect the whole file."""
    if profile_id == "proofreading" or role == "proofreading":
        return True
    return bool(_GLOBAL_INTENT_RE.search(prompt or ""))


def wants_edit_contract(profile_id: str, role: str) -> bool:
    """Editing profiles ask for SEARCH/REPLACE. Claim checks and math explainers do not."""
    if profile_id in _NO_EDIT_PROFILE_IDS:
        return False
    return role in _EDIT_ROLES


def _code_portion(line: str) -> str:
    return _COMMENT_RE.sub("", line)


def extract_preamble(tex: str) -> str:
    """Return the root file through the \\begin{document} line, or empty when there is none."""
    lines = tex.splitlines()
    for index, line in enumerate(lines):
        if "\\begin{document}" in _code_portion(line):
            return "\n".join(lines[: index + 1])
    return ""


def format_outline(nodes: list) -> str:
    """Flatten an outline tree into a short map. Nodes are OutlineNode-like."""
    lines: list[str] = []

    def walk(items: list, depth: int) -> None:
        for node in items:
            title = str(getattr(node, "title", "")).replace("\n", " ")
            level = getattr(node, "level", "section")
            file_path = getattr(node, "file_path", "")
            line_number = getattr(node, "line_number", 1)
            lines.append(f"{'  ' * depth}- {level}: {title} ({file_path}:{line_number})")
            children = getattr(node, "children", None) or []
            if children:
                walk(children, depth + 1)

    walk(nodes, 0)
    return "\n".join(lines)


def selection_span(text: str, selection: dict | None) -> tuple[int, int] | None:
    """1-based inclusive line span for a selection, preferring lines that still contain its text."""
    if not isinstance(selection, dict):
        return None
    lines = text.splitlines()
    total = len(lines)
    snippet = str(selection.get("text") or "")
    raw_start = selection.get("start_line")
    raw_end = selection.get("end_line")
    if total and raw_start:
        try:
            start = int(raw_start)
            end = int(raw_end if raw_end is not None else start)
        except (TypeError, ValueError):
            start = 0
            end = 0
        if 1 <= start <= total:
            end = max(start, min(end, total))
            sliced = "\n".join(lines[start - 1 : end])
            if not snippet or snippet in sliced or snippet.strip() in sliced:
                return start, end
    if snippet:
        used = snippet
        pos = text.find(used)
        if pos == -1:
            used = snippet.strip()
            pos = text.find(used) if used else -1
        if pos != -1:
            start = text.count("\n", 0, pos) + 1
            end = start + used.count("\n")
            return start, end
    return None


def _iter_code_lines(lines: list[str]):
    """Yield (line_number, raw, code) outside verbatim, listings, and minted blocks.

    Inside those blocks, % is literal, so the closer is read from the raw line.
    """
    stack: list[str] = []
    for index, line in enumerate(lines, start=1):
        code = _code_portion(line)
        if stack:
            end = _VERBATIM_END_RE.search(line)
            if end is not None and end.group("env") == stack[-1]:
                stack.pop()
            continue
        begin = _VERBATIM_BEGIN_RE.search(code)
        if begin is not None:
            stack.append(begin.group("env"))
            continue
        yield index, line, code


def _heading_title(code: str, match: re.Match[str]) -> str:
    depth = 1
    chars: list[str] = []
    escaped = False
    for ch in code[match.end() :]:
        if escaped:
            chars.append(ch)
            escaped = False
            continue
        if ch == "\\":
            chars.append(ch)
            escaped = True
            continue
        if ch == "{":
            depth += 1
            chars.append(ch)
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
            chars.append(ch)
        else:
            chars.append(ch)
    return "".join(chars).strip()


def section_bounds(text: str, cursor_line: int, max_lines: int = SECTION_MAX_LINES) -> tuple[int, int]:
    """Line span of the heading that contains the cursor, clipped when that span is long."""
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return 1, 1
    cursor = max(1, min(total, int(cursor_line or 1)))
    headings: list[tuple[int, int]] = []
    for index, _raw, code in _iter_code_lines(lines):
        match = _HEADING_RE.search(code)
        if match is not None:
            headings.append((index, _HEADING_RANK[match.group("cmd")]))

    start = 1
    rank: int | None = None
    for line_no, heading_rank in headings:
        if line_no <= cursor:
            start = line_no
            rank = heading_rank
        else:
            break
    end = total
    for line_no, heading_rank in headings:
        if line_no > start and (rank is None or heading_rank <= rank):
            end = line_no - 1
            break

    if end - start + 1 > max_lines:
        half = max_lines // 2
        new_start = max(start, cursor - half)
        new_end = min(end, new_start + max_lines - 1)
        if new_end - new_start + 1 < max_lines:
            new_start = max(start, new_end - max_lines + 1)
        start, end = new_start, new_end
    return start, end


def _slice(text: str, start: int, end: int) -> str:
    lines = text.splitlines()
    if not lines:
        return ""
    start = max(1, start)
    end = min(len(lines), end)
    return "\n".join(lines[start - 1 : end])


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def defuse_quote_markers(text: str, markers: tuple[str, ...]) -> str:
    """Replace this turn's end markers so quoted text cannot close a block early."""
    for marker in markers:
        text = text.replace(marker, _MARKER_PLACEHOLDER)
    return text


def _quoted_block(text: str, label: str, end: str, markers: tuple[str, ...]) -> str:
    """Wrap context so a closing fence or end marker inside the buffer cannot end the block."""
    return f"{label}\n{defuse_quote_markers(text, markers)}\n{end}"


def overlay_active_file_outline(nodes: list, active_path: str, active_text: str) -> list:
    """Use the unsaved buffer's headings for the active file. Other files stay on disk."""
    from app.latex.outline import OutlineNode

    active_norm = _norm(active_path)
    flat: list[OutlineNode] = []
    for index, _raw, code in _iter_code_lines(active_text.splitlines()):
        match = _OUTLINE_HEADING_RE.search(code)
        if match is None:
            continue
        level = match.group("cmd")
        flat.append(
            OutlineNode(
                title=_heading_title(code, match),
                level=level,  # type: ignore[arg-type]
                file_path=active_norm,
                line_number=index,
            )
        )
    buffer_nodes: list[OutlineNode] = []
    stack: list[OutlineNode] = []
    for node in flat:
        rank = _HEADING_RANK[node.level]
        while stack and _HEADING_RANK[stack[-1].level] >= rank:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            buffer_nodes.append(node)
        stack.append(node)

    inserted = False

    def walk(items: list) -> list:
        nonlocal inserted
        result = []
        for node in items:
            children = list(getattr(node, "children", None) or [])
            if _norm(getattr(node, "file_path", "")) == active_norm:
                kept_children = walk(children)
                if not inserted:
                    result.extend(buffer_nodes)
                    inserted = True
                result.extend(kept_children)
                continue
            if children:
                node.children = walk(children)
            result.append(node)
        return result

    merged = walk(list(nodes))
    if not inserted:
        merged.extend(buffer_nodes)
    return merged


def _has_begin_document(text: str) -> bool:
    return any("\\begin{document}" in code for _index, _raw, code in _iter_code_lines(text.splitlines()))


def build_manuscript_context(
    *,
    active_path: str,
    root_path: str,
    active_text: str,
    root_text: str,
    selection: dict | None = None,
    cursor_line: int | None = None,
    outline_text: str = "",
    user_prompt: str = "",
    profile_id: str = "",
    role: str = "",
    index_text: str = "",
    short_file_lines: int = SHORT_FILE_LINES,
    global_file_max_lines: int = GLOBAL_FILE_MAX_LINES,
    long_file_lines: int | None = None,
) -> ManuscriptContext:
    """Choose the editable window (or index-only map) and read-only blocks for this turn."""
    if long_file_lines is not None:
        short_file_lines = long_file_lines
        global_file_max_lines = long_file_lines

    active_norm = _norm(active_path)
    root_norm = _norm(root_path)
    total = len(active_text.splitlines())
    span = selection_span(active_text, selection)

    if span is not None:
        start, end = span
        start = max(1, start - SELECTION_PAD_LINES)
        end = min(max(total, 1), end + SELECTION_PAD_LINES)
        mode: ContextMode = "selection"
    elif cursor_line is not None and is_cursor_local_intent(user_prompt):
        start, end = section_bounds(active_text, cursor_line)
        mode = "section"
    elif total <= short_file_lines or (
        is_global_intent(user_prompt, profile_id, role) and total <= global_file_max_lines
    ):
        start, end = (1, total) if total else (1, 1)
        mode = "file"
    elif index_text.strip():
        start, end = 0, 0
        mode = "index"
    else:
        start, end = section_bounds(active_text, cursor_line or 1)
        mode = "section"

    window = "" if mode == "index" else _slice(active_text, start, end)
    preamble_source = active_text if active_norm == root_norm else root_text
    preamble = extract_preamble(preamble_source)
    window_has_preamble = (
        mode != "index"
        and active_norm == root_norm
        and (start == 1 or _has_begin_document(window))
    )

    nonce = secrets.token_hex(8)
    index_end = f"--- END PROJECT INDEX {nonce} ---"
    outline_end = f"--- END OUTLINE {nonce} ---"
    preamble_end = f"--- END PREAMBLE {nonce} ---"
    region_end = f"--- END EDITABLE REGION {nonce} ---"
    markers = (index_end, outline_end, preamble_end, region_end)

    parts: list[str] = []
    if mode == "index":
        parts.append(
            _quoted_block(
                index_text.strip(),
                "Project index (map only; inspect specific spans before editing):",
                index_end,
                markers,
            )
        )
    elif mode == "section" and outline_text.strip():
        parts.append(
            _quoted_block(
                outline_text.strip(),
                "Manuscript outline (map only; other sections are not included):",
                outline_end,
                markers,
            )
        )
    if preamble and not window_has_preamble:
        parts.append(
            _quoted_block(
                preamble,
                f"Read-only root preamble ({root_norm}). Do not edit this block.",
                preamble_end,
                markers,
            )
        )
    if mode != "index":
        parts.append(
            _quoted_block(
                window,
                f"Editable region ({active_norm} lines {start}-{end}):",
                region_end,
                markers,
            )
        )
    return ManuscriptContext(
        mode=mode,
        window_text=window,
        window_start=start,
        window_end=end,
        parts=parts,
        quote_markers=markers,
    )
