from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

OutlineLevel = Literal["part", "chapter", "section", "subsection", "subsubsection"]

_LEVEL_RANK: dict[OutlineLevel, int] = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
}

_COMMAND_PREFIX_RE = re.compile(
    r"\\(?P<cmd>part|chapter|section|subsection|subsubsection|input|include|subfile)\*?(?:\[[^\]]*\])?\{"
)

_UNESCAPED_COMMENT_RE = re.compile(r"(?<!\\)%.*$")


@dataclass
class OutlineNode:
    title: str
    level: OutlineLevel
    file_path: str
    line_number: int
    children: list[OutlineNode] = field(default_factory=list)


def _extract_commands(line: str) -> list[tuple[str, str]]:
    """Extract (cmd, arg) pairs handling balanced nested braces like \\section{\\textbf{Title}}."""
    results: list[tuple[str, str]] = []
    pos = 0
    while pos < len(line):
        match = _COMMAND_PREFIX_RE.search(line, pos)
        if not match:
            break
        cmd = match.group("cmd")
        start_brace = match.end() - 1
        depth = 0
        arg_chars: list[str] = []
        i = start_brace
        matched_end = False
        while i < len(line):
            ch = line[i]
            if ch == "{" and (i == 0 or line[i - 1] != "\\"):
                depth += 1
                if depth > 1:
                    arg_chars.append(ch)
            elif ch == "}" and (i == 0 or line[i - 1] != "\\"):
                depth -= 1
                if depth == 0:
                    matched_end = True
                    pos = i + 1
                    break
                else:
                    arg_chars.append(ch)
            else:
                arg_chars.append(ch)
            i += 1
        if matched_end:
            results.append((cmd, "".join(arg_chars).strip()))
        else:
            pos = match.end()
    return results


def _strip_comment(line: str) -> str:
    return _UNESCAPED_COMMENT_RE.sub("", line)


def _resolve_included_tex(
    project_root: Path,
    current_file: Path,
    raw_target: str,
) -> Path | None:
    cleaned = raw_target.strip()
    if not cleaned:
        return None

    candidates = [
        current_file.parent / cleaned,
        project_root / cleaned,
    ]
    if not Path(cleaned).suffix:
        candidates = [
            current_file.parent / f"{cleaned}.tex",
            project_root / f"{cleaned}.tex",
            *candidates,
        ]

    for candidate in candidates:
        try:
            # Reject symlinks anywhere along the path inside project_root
            curr = candidate
            has_symlink = False
            while curr != project_root and curr != curr.parent:
                if curr.is_symlink():
                    has_symlink = True
                    break
                curr = curr.parent
            if has_symlink:
                continue

            resolved = candidate.resolve()
            if not resolved.is_relative_to(project_root):
                continue
            if resolved.is_file():
                return resolved
        except (OSError, ValueError):
            continue
    return None


def _collect_flat_nodes(
    project_root: Path,
    tex_file: Path,
    active_stack: set[Path],
    out_nodes: list[OutlineNode],
) -> None:
    if tex_file in active_stack:
        return
    if not tex_file.is_file() or not tex_file.is_relative_to(project_root):
        return

    active_stack.add(tex_file)
    try:
        rel_posix = tex_file.relative_to(project_root).as_posix()
        lines = tex_file.read_text(encoding="utf-8", errors="replace").splitlines()
        for line_idx, raw_line in enumerate(lines, start=1):
            clean_line = _strip_comment(raw_line)
            if not clean_line.strip():
                continue
            for cmd, arg in _extract_commands(clean_line):
                if cmd in {"input", "include", "subfile"}:
                    child_file = _resolve_included_tex(project_root, tex_file, arg)
                    if child_file is not None:
                        _collect_flat_nodes(project_root, child_file, active_stack, out_nodes)
                else:
                    level: OutlineLevel = cmd  # type: ignore[assignment]
                    out_nodes.append(
                        OutlineNode(
                            title=arg,
                            level=level,
                            file_path=rel_posix,
                            line_number=line_idx,
                        )
                    )
    finally:
        active_stack.remove(tex_file)


def parse_project_outline(project_dir: Path, root_tex: str) -> list[OutlineNode]:
    """Parse a hierarchical section outline starting from root_tex across nested \\input/\\include files."""
    project_root = Path(project_dir).resolve()
    root_candidate = (project_root / root_tex.lstrip("/")).resolve()
    if not root_candidate.is_relative_to(project_root) or not root_candidate.is_file():
        return []

    flat_nodes: list[OutlineNode] = []
    _collect_flat_nodes(
        project_root=project_root,
        tex_file=root_candidate,
        active_stack=set(),
        out_nodes=flat_nodes,
    )

    root_nodes: list[OutlineNode] = []
    stack: list[OutlineNode] = []

    for node in flat_nodes:
        node_rank = _LEVEL_RANK[node.level]
        while stack and _LEVEL_RANK[stack[-1].level] >= node_rank:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            root_nodes.append(node)
        stack.append(node)

    return root_nodes
