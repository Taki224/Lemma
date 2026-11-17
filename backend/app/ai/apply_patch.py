"""Apply OpenRouter / OpenAI V4A update diffs and turn them into reviewable spans.

OpenRouter validates the diff, then returns it on an apply_patch_call. This module
applies that diff in memory. It does not write files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher


class ApplyPatchError(ValueError):
    """The diff is not a V4A update, or it does not match the file."""


@dataclass
class _Chunk:
    change_context: str | None
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    is_end_of_file: bool = False


def paths_refer_to_same_file(expected: str, actual: str) -> bool:
    """True when `actual` is the active file, possibly with a directory prefix."""

    def norm(path: str) -> str:
        value = path.strip().replace("\\", "/")
        while value.startswith("./"):
            value = value[2:]
        return value.strip("/")

    wanted = norm(expected)
    got = norm(actual)
    if not wanted or not got:
        return False
    return wanted == got or got.endswith("/" + wanted)


def apply_v4a_update(original: str, diff: str) -> str:
    """Return the file text after applying one update_file V4A diff."""
    chunks = _parse_update_chunks(diff)
    lines = original.splitlines()
    cursor = 0
    for chunk in chunks:
        if chunk.change_context is not None:
            found = _seek_line(lines, chunk.change_context, cursor)
            if found is None:
                raise ApplyPatchError(
                    f"could not find @@ context {chunk.change_context!r}"
                )
            cursor = found + 1
        if not chunk.old_lines and not chunk.new_lines:
            raise ApplyPatchError("patch hunk is empty")
        if not chunk.old_lines:
            insert_at = len(lines) if chunk.is_end_of_file else cursor
            lines[insert_at:insert_at] = chunk.new_lines
            cursor = insert_at + len(chunk.new_lines)
            continue
        found = _seek_sequence(lines, chunk.old_lines, cursor, eof=chunk.is_end_of_file)
        if found is None:
            raise ApplyPatchError("could not locate the lines the diff removes")
        lines[found : found + len(chunk.old_lines)] = chunk.new_lines
        cursor = found + len(chunk.new_lines)

    updated = "\n".join(lines)
    if lines and original.endswith("\n"):
        updated += "\n"
    return updated


def changed_line_spans(
    before: str, after: str
) -> list[tuple[int, int, str, str]]:
    """Line spans a reviewer can anchor: (start_line, end_line, original, replacement).

    Insertions borrow one unchanged neighbour so the original text is never empty
    unless the file itself was empty.
    """
    old = before.splitlines()
    new = after.splitlines()
    if old == new:
        return []

    ops = [
        op
        for op in SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes()
        if op[0] != "equal"
    ]
    consumed = {index for _, i1, i2, _, _ in ops for index in range(i1, i2)}
    raw: list[tuple[int, int, int, int]] = []
    for _, i1, i2, j1, j2 in ops:
        oi1, oi2, nj1, nj2 = i1, i2, j1, j2
        if oi1 == oi2:
            if oi1 > 0 and (oi1 - 1) not in consumed:
                oi1 -= 1
                nj1 -= 1
                consumed.add(oi1)
            elif oi2 < len(old) and oi2 not in consumed:
                oi2 += 1
                nj2 += 1
                consumed.add(oi2 - 1)
        raw.append((oi1, oi2, nj1, nj2))

    merged: list[tuple[int, int, int, int]] = []
    for span in raw:
        if (
            merged
            and span[0] <= merged[-1][1]
        ):
            prev = merged[-1]
            merged[-1] = (
                prev[0],
                max(prev[1], span[1]),
                prev[2],
                max(prev[3], span[3]),
            )
            continue
        merged.append(span)

    spans: list[tuple[int, int, str, str]] = []
    for oi1, oi2, nj1, nj2 in merged:
        original = "\n".join(old[oi1:oi2])
        replacement = "\n".join(new[nj1:nj2])
        if original == replacement:
            continue
        start = oi1 + 1 if oi2 else 1
        end = oi2 if oi2 >= 1 else 1
        spans.append((start, end, original, replacement))
    return spans


def _parse_update_chunks(diff: str) -> list[_Chunk]:
    text = diff.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if any(
        line.strip().startswith("*** Update File:") or line.strip() == "*** Begin Patch"
        for line in lines
    ):
        lines = _unwrap_update_body(lines)

    chunks: list[_Chunk] = []
    current: _Chunk | None = None
    for line in lines:
        if line.startswith("@@"):
            if line == "@@":
                context: str | None = None
            elif line.startswith("@@ "):
                context = line[3:]
            else:
                raise ApplyPatchError(f"invalid @@ line: {line!r}")
            current = _Chunk(change_context=context)
            chunks.append(current)
            continue
        if line.strip() == "*** End of File":
            if current is None:
                raise ApplyPatchError("end-of-file marker without a hunk")
            current.is_end_of_file = True
            continue
        if current is None:
            current = _Chunk(change_context=None)
            chunks.append(current)
        if line.startswith("+"):
            current.new_lines.append(line[1:])
        elif line.startswith("-"):
            current.old_lines.append(line[1:])
        elif line.startswith(" "):
            content = line[1:]
            current.old_lines.append(content)
            current.new_lines.append(content)
        elif line == "":
            current.old_lines.append("")
            current.new_lines.append("")
        else:
            raise ApplyPatchError(f"invalid diff line: {line!r}")

    if not chunks:
        raise ApplyPatchError("patch has no hunks")
    return chunks


def _unwrap_update_body(lines: list[str]) -> list[str]:
    body: list[str] = []
    in_update = False
    saw_update = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("*** Move to:"):
            raise ApplyPatchError("file renames are not applied")
        if stripped.startswith("*** Add File:") or stripped.startswith("*** Delete File:"):
            raise ApplyPatchError("only updates to an existing file are applied")
        if stripped.startswith("*** Update File:"):
            if saw_update:
                raise ApplyPatchError("diff updates more than one file")
            saw_update = True
            in_update = True
            continue
        if stripped == "*** End Patch":
            break
        if stripped == "*** Begin Patch" or stripped.startswith("*** Environment ID:"):
            continue
        if stripped.startswith("*** ") and stripped != "*** End of File":
            raise ApplyPatchError(f"unsupported patch marker: {stripped}")
        if in_update:
            body.append(line)
    if not saw_update:
        raise ApplyPatchError("patch does not update a file")
    return body


def _norm_exact(value: str) -> str:
    return value


def _norm_rstrip(value: str) -> str:
    return value.rstrip()


def _norm_strip(value: str) -> str:
    return value.strip()


_NORMS = (_norm_exact, _norm_rstrip, _norm_strip)


def _seek_line(lines: list[str], needle: str, start: int) -> int | None:
    for norm in _NORMS:
        target = norm(needle)
        for index in range(start, len(lines)):
            if norm(lines[index]) == target:
                return index
    return None


def _at_eof(lines: list[str], end: int) -> bool:
    return end == len(lines) or all(not lines[index].strip() for index in range(end, len(lines)))


def _seek_sequence(
    lines: list[str], pattern: list[str], start: int, *, eof: bool
) -> int | None:
    if start > len(lines):
        return None
    width = len(pattern)
    last = len(lines) - width
    for norm in _NORMS:
        wanted = [norm(line) for line in pattern]
        for index in range(start, last + 1):
            if [norm(lines[index + offset]) for offset in range(width)] != wanted:
                continue
            if eof and not _at_eof(lines, index + width):
                continue
            return index
    return None
