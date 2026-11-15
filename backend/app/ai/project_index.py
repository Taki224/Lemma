"""Deterministic .tex and .bib parser and chunker for project indexing."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from app.ai.manuscript_context import _HEADING_RANK, _HEADING_RE, _heading_title, _iter_code_lines
from app.db import DatabaseManager
from app.projects.service import ProjectService

_INDEXABLE_EXTENSIONS = frozenset({".tex", ".bib"})

CHUNK_TARGET_LINES = 40

EntityKind = Literal["heading", "label", "env", "macro", "cite"]

_LABEL_RE = re.compile(r"\\label\{([^{}]+)\}")
_MACRO_RE = re.compile(
    r"\\(?P<cmd>newcommand|renewcommand|providecommand|DeclareMathOperator)\*?\s*"
    r"(?:\{(?P<braced>\\[A-Za-z@]+)\}|(?P<bare>\\[A-Za-z@]+))"
)
_CITE_RE = re.compile(
    r"\\(?:[Cc]ite[a-zA-Z]*|nocite|autocite|textcite|parencite|footcite)\*?"
    r"(?:\s*\[[^\]]*\]){0,2}\s*\{(?P<keys>[^{}]+)\}"
)
_ENV_TOKEN_RE = re.compile(
    r"\\(?P<kind>begin|end)\{(?P<env>[A-Za-z*]+)\}(?:\s*\[(?P<opt>[^\]]*)\])?"
)

_TRACKED_ENVS = frozenset(
    {
        "theorem",
        "lemma",
        "proposition",
        "corollary",
        "definition",
        "remark",
        "example",
        "proof",
        "claim",
        "conjecture",
        "axiom",
        "figure",
        "figure*",
        "table",
        "table*",
        "equation",
        "equation*",
        "align",
        "align*",
        "gather",
        "gather*",
        "multline",
        "multline*",
        "split",
        "cases",
        "abstract",
    }
)

_BIB_ENTRY_RE = re.compile(
    r"@(?P<type>[A-Za-z]+)\s*(?P<delim>[{(])\s*(?P<key>[^,\s{}()]+)\s*,"
)
_BIB_SKIP_TYPES = frozenset({"comment", "preamble", "string"})
_BIB_TITLE_RE = re.compile(r"\btitle\s*=\s*[{\"](.+?)[}\"]\s*,?\s*$", re.IGNORECASE)

_UPSERT_FILE_SQL = """
INSERT INTO project_index_files (
    project_id, rel_path, content_hash, mtime_ns, line_count, index_json, updated_at
)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(project_id, rel_path) DO UPDATE SET
    content_hash = excluded.content_hash,
    mtime_ns = excluded.mtime_ns,
    line_count = excluded.line_count,
    index_json = excluded.index_json,
    updated_at = excluded.updated_at;
"""


@dataclass(frozen=True)
class IndexedEntity:
    kind: EntityKind
    name: str
    file_path: str
    start_line: int
    end_line: int
    detail: str = ""


@dataclass(frozen=True)
class IndexedChunk:
    file_path: str
    start_line: int
    end_line: int
    text: str
    heading: str = ""


@dataclass(frozen=True)
class FileIndex:
    rel_path: str
    content_hash: str
    mtime_ns: int
    line_count: int
    entities: list[IndexedEntity]
    chunks: list[IndexedChunk]


@dataclass
class ProjectIndex:
    project_id: str
    files: dict[str, FileIndex] = field(default_factory=dict)


@dataclass(frozen=True)
class IndexSearchHit:
    file_path: str
    start_line: int
    end_line: int
    score: float
    reason: str
    preview: str


def _chunk_tex_lines(
    rel_path: str, lines: list[str], headings: list[tuple[int, str]]
) -> list[IndexedChunk]:
    total = len(lines)
    if total == 0:
        return []

    segments: list[tuple[int, int, str]] = []
    if not headings:
        segments.append((1, total, ""))
    else:
        if headings[0][0] > 1:
            segments.append((1, headings[0][0] - 1, ""))
        for idx, (h_line, h_title) in enumerate(headings):
            seg_end = headings[idx + 1][0] - 1 if idx + 1 < len(headings) else total
            if h_line <= seg_end:
                segments.append((h_line, seg_end, h_title))

    chunks: list[IndexedChunk] = []
    for seg_start, seg_end, heading_title in segments:
        pos = seg_start
        while pos <= seg_end:
            end = min(seg_end, pos + CHUNK_TARGET_LINES - 1)
            chunks.append(
                IndexedChunk(
                    file_path=rel_path,
                    start_line=pos,
                    end_line=end,
                    text="\n".join(lines[pos - 1 : end]),
                    heading=heading_title,
                )
            )
            pos = end + 1
    return chunks


def index_tex_text(
    rel_path: str,
    text: str,
    *,
    content_hash: str = "",
    mtime_ns: int = 0,
) -> FileIndex:
    """Parse a LaTeX file into entities and heading-bounded line chunks."""
    lines = text.splitlines()
    entities: list[IndexedEntity] = []
    headings: list[tuple[int, str]] = []
    env_stack: list[tuple[str, int, str]] = []

    for line_no, _raw, code in _iter_code_lines(lines):
        for match in _HEADING_RE.finditer(code):
            title = _heading_title(code, match)
            headings.append((line_no, title))
            entities.append(
                IndexedEntity(
                    kind="heading",
                    name=title,
                    file_path=rel_path,
                    start_line=line_no,
                    end_line=line_no,
                    detail=match.group("cmd"),
                )
            )

        for match in _LABEL_RE.finditer(code):
            label = match.group(1).strip()
            if label:
                entities.append(
                    IndexedEntity(
                        kind="label",
                        name=label,
                        file_path=rel_path,
                        start_line=line_no,
                        end_line=line_no,
                    )
                )

        for match in _ENV_TOKEN_RE.finditer(code):
            env_name = match.group("env")
            if env_name not in _TRACKED_ENVS:
                continue
            if match.group("kind") == "begin":
                opt = (match.group("opt") or "").strip()
                env_stack.append((env_name, line_no, opt))
            else:
                for i in range(len(env_stack) - 1, -1, -1):
                    if env_stack[i][0] == env_name:
                        _, begin_line, opt = env_stack.pop(i)
                        entities.append(
                            IndexedEntity(
                                kind="env",
                                name=env_name,
                                file_path=rel_path,
                                start_line=begin_line,
                                end_line=line_no,
                                detail=opt,
                            )
                        )
                        break

        for match in _MACRO_RE.finditer(code):
            macro_name = match.group("braced") or match.group("bare") or ""
            if macro_name:
                entities.append(
                    IndexedEntity(
                        kind="macro",
                        name=macro_name,
                        file_path=rel_path,
                        start_line=line_no,
                        end_line=line_no,
                        detail=match.group("cmd"),
                    )
                )

        for match in _CITE_RE.finditer(code):
            for raw_key in match.group("keys").split(","):
                key = raw_key.strip()
                if key and key != "*":
                    entities.append(
                        IndexedEntity(
                            kind="cite",
                            name=key,
                            file_path=rel_path,
                            start_line=line_no,
                            end_line=line_no,
                        )
                    )

    for env_name, begin_line, opt in env_stack:
        entities.append(
            IndexedEntity(
                kind="env",
                name=env_name,
                file_path=rel_path,
                start_line=begin_line,
                end_line=len(lines),
                detail=opt,
            )
        )

    chunks = _chunk_tex_lines(rel_path, lines, headings)
    return FileIndex(
        rel_path=rel_path,
        content_hash=content_hash,
        mtime_ns=mtime_ns,
        line_count=len(lines),
        entities=entities,
        chunks=chunks,
    )


def _strip_unescaped_comment(line: str) -> str:
    """Strip an unescaped % comment to end of line."""
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "%":
            return line[:i]
    return line


def _scan_bib_entry_end(
    lines: list[str], start_idx: int, start_col: int, open_ch: str
) -> int:
    """Return 1-based end line of a BibTeX entry starting at (start_idx, start_col)."""
    close_ch = "}" if open_ch == "{" else ")"
    depth = 0
    for idx in range(start_idx, len(lines)):
        line = _strip_unescaped_comment(lines[idx])
        col_start = start_col if idx == start_idx else 0
        escaped = False
        for ch in line[col_start:]:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return idx + 1
    return len(lines)


def _bib_entry_detail(entry_type: str, entry_lines: list[str]) -> str:
    for line in entry_lines:
        match = _BIB_TITLE_RE.search(line.strip())
        if match is not None:
            title = match.group(1).replace("{", "").replace("}", "").strip()
            return f"{entry_type}: {title}"
    return entry_type


def index_bib_text(
    rel_path: str,
    text: str,
    *,
    content_hash: str = "",
    mtime_ns: int = 0,
) -> FileIndex:
    """Parse a BibTeX file into citation entities and entry chunks."""
    lines = text.splitlines()
    entities: list[IndexedEntity] = []
    chunks: list[IndexedChunk] = []

    idx = 0
    total = len(lines)
    while idx < total:
        code_line = _strip_unescaped_comment(lines[idx])
        match = _BIB_ENTRY_RE.search(code_line)
        if match is None:
            idx += 1
            continue

        entry_type = match.group("type").lower()
        open_ch = match.group("delim")
        key = match.group("key").strip()
        start_line = idx + 1
        end_line = _scan_bib_entry_end(lines, idx, match.start("delim"), open_ch)

        if entry_type not in _BIB_SKIP_TYPES and key:
            entry_lines = lines[start_line - 1 : end_line]
            entities.append(
                IndexedEntity(
                    kind="cite",
                    name=key,
                    file_path=rel_path,
                    start_line=start_line,
                    end_line=end_line,
                    detail=_bib_entry_detail(entry_type, entry_lines),
                )
            )
            chunks.append(
                IndexedChunk(
                    file_path=rel_path,
                    start_line=start_line,
                    end_line=end_line,
                    text="\n".join(entry_lines),
                    heading=key,
                )
            )

        idx = end_line

    return FileIndex(
        rel_path=rel_path,
        content_hash=content_hash,
        mtime_ns=mtime_ns,
        line_count=total,
        entities=entities,
        chunks=chunks,
    )


def _normalize_rel_path(rel_path: str) -> str:
    cleaned = rel_path.strip().replace("\\", "/")
    if not cleaned:
        return ""
    norm = posixpath.normpath(cleaned).lstrip("/")
    return "" if norm == "." else norm


def _is_indexable_path(rel_path: str) -> bool:
    return bool(rel_path) and Path(rel_path).suffix.lower() in _INDEXABLE_EXTENSIONS


def _escape_like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _index_text_for_ext(
    rel_path: str,
    text: str,
    *,
    content_hash: str,
    mtime_ns: int,
) -> FileIndex | None:
    ext = Path(rel_path).suffix.lower()
    if ext == ".tex":
        return index_tex_text(rel_path, text, content_hash=content_hash, mtime_ns=mtime_ns)
    if ext == ".bib":
        return index_bib_text(rel_path, text, content_hash=content_hash, mtime_ns=mtime_ns)
    return None


def _serialize_file_index(file_index: FileIndex) -> str:
    return json.dumps(
        {
            "entities": [asdict(e) for e in file_index.entities],
            "chunks": [asdict(c) for c in file_index.chunks],
        }
    )


def _deserialize_file_index(row: object) -> FileIndex:
    payload = json.loads(row["index_json"])  # type: ignore[index]
    entities = [IndexedEntity(**e) for e in payload.get("entities", [])]
    chunks = [IndexedChunk(**c) for c in payload.get("chunks", [])]
    return FileIndex(
        rel_path=str(row["rel_path"]),  # type: ignore[index]
        content_hash=str(row["content_hash"]),  # type: ignore[index]
        mtime_ns=int(row["mtime_ns"]),  # type: ignore[index]
        line_count=int(row["line_count"]),  # type: ignore[index]
        entities=entities,
        chunks=chunks,
    )


class ProjectIndexStore:
    """SQLite persistence, staleness rebuild, and in-memory buffer overlay for ProjectIndex."""

    def __init__(self, db: DatabaseManager, project_service: ProjectService) -> None:
        self._db = db
        self._project_service = project_service

    def upsert_file(
        self, project_id: str, rel_path: str, text: str | None = None
    ) -> FileIndex | None:
        """Index a .tex/.bib file and upsert its entry in project_index_files."""
        clean_path = _normalize_rel_path(rel_path)
        if not _is_indexable_path(clean_path):
            return None

        project_dir = self._project_service.get_project_dir(project_id)
        target = self._project_service._validate_project_rel_path(project_dir, clean_path)
        rel_posix = target.relative_to(project_dir.resolve()).as_posix()

        if text is None:
            if not target.is_file():
                raise FileNotFoundError(f"File not found: {rel_path}")
            raw_bytes = target.read_bytes()
            text = raw_bytes.decode("utf-8", errors="replace")
            mtime_ns = target.stat().st_mtime_ns
        else:
            raw_bytes = text.encode("utf-8")
            mtime_ns = target.stat().st_mtime_ns if target.is_file() else 0

        content_hash = hashlib.sha256(raw_bytes).hexdigest()
        file_index = _index_text_for_ext(
            rel_posix,
            text,
            content_hash=content_hash,
            mtime_ns=mtime_ns,
        )
        if file_index is None:
            return None

        now_iso = datetime.now(timezone.utc).isoformat()
        index_json = _serialize_file_index(file_index)
        with closing(self._db.get_connection()) as conn:
            conn.execute(
                _UPSERT_FILE_SQL,
                (
                    project_id,
                    file_index.rel_path,
                    file_index.content_hash,
                    file_index.mtime_ns,
                    file_index.line_count,
                    index_json,
                    now_iso,
                ),
            )
        return file_index

    def delete_file(self, project_id: str, rel_path: str) -> None:
        """Delete indexed row(s) matching rel_path or folder prefix."""
        clean_path = _normalize_rel_path(rel_path)
        if not clean_path:
            return
        like_pattern = f"{_escape_like_prefix(clean_path)}/%"
        with closing(self._db.get_connection()) as conn:
            conn.execute(
                """
                DELETE FROM project_index_files
                WHERE project_id = ?
                  AND (rel_path = ? OR rel_path LIKE ? ESCAPE '\\');
                """,
                (project_id, clean_path, like_pattern),
            )

    def move_path(self, project_id: str, from_path: str, to_path: str) -> None:
        """Re-key moved file(s) or folder prefix in SQLite and update file_path in JSON."""
        from_clean = _normalize_rel_path(from_path)
        to_clean = _normalize_rel_path(to_path)
        if not from_clean or not to_clean or from_clean == to_clean:
            return

        like_pattern = f"{_escape_like_prefix(from_clean)}/%"
        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(self._db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                rows = conn.execute(
                    """
                    SELECT rel_path, content_hash, mtime_ns, line_count, index_json
                    FROM project_index_files
                    WHERE project_id = ?
                      AND (rel_path = ? OR rel_path LIKE ? ESCAPE '\\');
                    """,
                    (project_id, from_clean, like_pattern),
                ).fetchall()

                updates: list[tuple[str, str, str, int, int, str]] = []
                deletes: list[str] = []
                for row in rows:
                    old_rel = str(row["rel_path"])
                    new_rel = _normalize_rel_path(to_clean + old_rel[len(from_clean) :])
                    if not _is_indexable_path(new_rel):
                        deletes.append(old_rel)
                        continue
                    payload = json.loads(row["index_json"])
                    for entity in payload.get("entities", []):
                        entity["file_path"] = new_rel
                    for chunk in payload.get("chunks", []):
                        chunk["file_path"] = new_rel
                    updates.append(
                        (
                            old_rel,
                            new_rel,
                            str(row["content_hash"]),
                            int(row["mtime_ns"]),
                            int(row["line_count"]),
                            json.dumps(payload),
                        )
                    )

                for old_rel in [*(u[0] for u in updates), *deletes]:
                    conn.execute(
                        "DELETE FROM project_index_files WHERE project_id = ? AND rel_path = ?;",
                        (project_id, old_rel),
                    )
                for _old_rel, new_rel, content_hash, mtime_ns, line_count, new_json in updates:
                    conn.execute(
                        _UPSERT_FILE_SQL,
                        (project_id, new_rel, content_hash, mtime_ns, line_count, new_json, now_iso),
                    )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def load_or_rebuild(self, project_id: str) -> ProjectIndex:
        """Load project index from SQLite, rebuilding any missing/stale files and pruning deleted ones."""
        disk_nodes = self._project_service.list_files(project_id)
        disk_by_path = {
            node.path: node
            for node in disk_nodes
            if _is_indexable_path(node.path)
        }
        project_dir = self._project_service.get_project_dir(project_id).resolve()

        with closing(self._db.get_connection()) as conn:
            rows = conn.execute(
                """
                SELECT rel_path, content_hash, mtime_ns, line_count, index_json
                FROM project_index_files
                WHERE project_id = ?;
                """,
                (project_id,),
            ).fetchall()

        stored_by_path: dict[str, FileIndex] = {}
        stored_paths: set[str] = set()
        for row in rows:
            rel_path = str(row["rel_path"])
            stored_paths.add(rel_path)
            try:
                stored_by_path[rel_path] = _deserialize_file_index(row)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue

        deleted_paths = stored_paths - set(disk_by_path)
        for rel_path in deleted_paths:
            self.delete_file(project_id, rel_path)

        result_files: dict[str, FileIndex] = {}
        for rel_path in sorted(disk_by_path):
            node = disk_by_path[rel_path]
            existing = stored_by_path.get(rel_path)
            try:
                disk_mtime_ns = (project_dir / rel_path).stat().st_mtime_ns
            except FileNotFoundError:
                self.delete_file(project_id, rel_path)
                continue
            if (
                existing is not None
                and existing.mtime_ns == disk_mtime_ns
                and existing.content_hash == (node.etag or "")
            ):
                result_files[rel_path] = existing
            else:
                try:
                    rebuilt = self.upsert_file(project_id, rel_path)
                except FileNotFoundError:
                    self.delete_file(project_id, rel_path)
                    continue
                if rebuilt is not None:
                    result_files[rel_path] = rebuilt

        return ProjectIndex(project_id=project_id, files=result_files)

    def overlay_buffers(
        self, index: ProjectIndex, buffers: dict[str, str]
    ) -> ProjectIndex:
        """Return a shallow copy of ProjectIndex with unsaved .tex/.bib buffers re-indexed in memory."""
        new_files = dict(index.files)
        for raw_path, text in buffers.items():
            rel_path = _normalize_rel_path(raw_path)
            if not _is_indexable_path(rel_path):
                continue
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            prev = new_files.get(rel_path)
            mtime_ns = prev.mtime_ns if prev is not None else 0
            reindexed = _index_text_for_ext(
                rel_path,
                text,
                content_hash=content_hash,
                mtime_ns=mtime_ns,
            )
            if reindexed is not None:
                new_files[rel_path] = reindexed
        return ProjectIndex(project_id=index.project_id, files=new_files)


_TOKEN_RE = re.compile(r"\\[A-Za-z@]+|[A-Za-z0-9_.:/-]+")


def _tokenize_query(text: str) -> list[str]:
    """Tokenize a query preserving LaTeX commands and labels, deduplicated in order."""
    seen: set[str] = set()
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        tok = match.group(0).casefold()
        if tok not in seen:
            seen.add(tok)
            tokens.append(tok)
    return tokens


def _tokenize_text(text: str) -> set[str]:
    """Tokenize text into a set of casefolded tokens."""
    return {match.group(0).casefold() for match in _TOKEN_RE.finditer(text)}


def _matches_path_filter(rel_path: str, norm_filter: str) -> bool:
    if not norm_filter:
        return True
    return rel_path == norm_filter or rel_path.startswith(f"{norm_filter}/")


def _extract_preview(
    file_index: FileIndex, start_line: int, end_line: int, max_len: int = 200
) -> str:
    """Build a compact single-line preview from chunks overlapping [start_line, end_line]."""
    snippets: list[str] = []
    for chunk in file_index.chunks:
        if chunk.end_line < start_line or chunk.start_line > end_line:
            continue
        chunk_lines = chunk.text.splitlines()
        lo = max(start_line, chunk.start_line) - chunk.start_line
        hi = min(end_line, chunk.end_line) - chunk.start_line
        for raw in chunk_lines[lo : hi + 1]:
            stripped = raw.strip()
            if stripped:
                snippets.append(stripped)
            if len(" ".join(snippets)) >= max_len:
                break
        if len(" ".join(snippets)) >= max_len:
            break
    joined = " ".join(snippets)
    if len(joined) <= max_len:
        return joined
    return joined[: max_len - 3].rstrip() + "..."


def _score_entity(
    entity: IndexedEntity, query_cf: str, tokens: list[str]
) -> tuple[float, str]:
    name_cf = entity.name.casefold()
    if name_cf == query_cf:
        return 100.0, f"exact {entity.kind} match: {entity.name}"

    matched_name = sum(1 for tok in tokens if tok in name_cf)
    if len(tokens) > 1 and (
        matched_name > 1 or (" " in name_cf and name_cf in query_cf)
    ):
        score = 88.0 + 10.0 * (matched_name / len(tokens))
        if name_cf in query_cf:
            score = min(99.0, score + 1.0)
        return score, f"multi-token {entity.kind} match: {entity.name}"

    if name_cf in tokens:
        return 85.0, f"exact {entity.kind} token match: {entity.name}"
    if query_cf in name_cf:
        tightness = len(query_cf) / max(len(name_cf), 1)
        return 65.0 + 10.0 * tightness, f"{entity.kind} match: {entity.name}"

    if matched_name > 0:
        return (
            45.0 + 15.0 * (matched_name / len(tokens)),
            f"{entity.kind} partial match: {entity.name}",
        )

    detail_cf = entity.detail.casefold()
    if detail_cf:
        if query_cf in detail_cf:
            return 35.0, f"{entity.kind} detail match: {entity.detail}"
        matched_detail = sum(1 for tok in tokens if tok in detail_cf)
        if matched_detail > 0:
            return (
                25.0 + 10.0 * (matched_detail / len(tokens)),
                f"{entity.kind} detail match: {entity.detail}",
            )

    return 0.0, ""


def _score_chunk(
    chunk: IndexedChunk, query_cf: str, tokens: list[str]
) -> tuple[float, str]:
    text_cf = chunk.text.casefold()
    chunk_tokens = _tokenize_text(chunk.text)
    matched = sum(1 for tok in tokens if tok in chunk_tokens)
    has_phrase = query_cf in text_cf
    if matched == 0 and not has_phrase:
        return 0.0, ""
    score = 10.0 * (matched / len(tokens))
    if has_phrase:
        score += 5.0 + 0.5 * min(text_cf.count(query_cf), 5)
    return score, f"chunk text match ({matched}/{len(tokens)} tokens)"


def search_project_index(
    index: ProjectIndex,
    query: str,
    *,
    path_filter: str | None = None,
    active_path: str = "",
    limit: int = 8,
) -> list[IndexSearchHit]:
    """Deterministically search indexed entities and chunks, deduplicating overlapping spans."""
    if limit <= 0:
        return []
    query_cf = query.strip().casefold()
    if not query_cf:
        return []

    tokens = _tokenize_query(query)
    if not tokens:
        tokens = [query_cf]

    norm_filter = _normalize_rel_path(path_filter) if path_filter else ""
    norm_active = _normalize_rel_path(active_path) if active_path else ""

    candidates: list[IndexSearchHit] = []
    for rel_path, file_index in index.files.items():
        if not _matches_path_filter(rel_path, norm_filter):
            continue

        for entity in file_index.entities:
            score, reason = _score_entity(entity, query_cf, tokens)
            if score <= 0.0:
                continue
            preview = _extract_preview(file_index, entity.start_line, entity.end_line)
            if not preview:
                preview = f"[{entity.kind}] {entity.name}"
            candidates.append(
                IndexSearchHit(
                    file_path=entity.file_path,
                    start_line=entity.start_line,
                    end_line=entity.end_line,
                    score=score,
                    reason=reason,
                    preview=preview,
                )
            )

        for chunk in file_index.chunks:
            score, reason = _score_chunk(chunk, query_cf, tokens)
            if score <= 0.0:
                continue
            preview = _extract_preview(file_index, chunk.start_line, chunk.end_line)
            candidates.append(
                IndexSearchHit(
                    file_path=chunk.file_path,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    score=score,
                    reason=reason,
                    preview=preview,
                )
            )

    candidates.sort(
        key=lambda hit: (
            -round(hit.score, 6),
            0 if hit.file_path == norm_active else 1,
            hit.start_line,
            hit.end_line,
            hit.file_path,
        )
    )

    kept: list[IndexSearchHit] = []
    kept_ranges: dict[str, list[tuple[int, int]]] = {}
    for hit in candidates:
        ranges = kept_ranges.setdefault(hit.file_path, [])
        if any(s <= hit.end_line and hit.start_line <= e for s, e in ranges):
            continue
        ranges.append((hit.start_line, hit.end_line))
        kept.append(hit)
        if len(kept) >= limit:
            break
    return kept


def _trim_section_lines(lines: list[str], budget: int) -> str:
    """Render a section within budget, trimming trailing items with '... (+N more)'."""
    if budget <= 0 or not lines:
        return ""
    full = "\n".join(lines)
    if len(full) <= budget:
        return full

    header = lines[0]
    items = lines[1:]
    if not items:
        return header if len(header) <= budget else ""

    kept: list[str] = [header]
    used = len(header)
    for idx, item in enumerate(items):
        remaining_after = len(items) - (idx + 1)
        note = f"\n... (+{remaining_after} more)" if remaining_after > 0 else ""
        if used + 1 + len(item) + len(note) <= budget:
            kept.append(item)
            used += 1 + len(item)
        else:
            omitted = len(items) - idx
            omitted_note = f"... (+{omitted} more)"
            if used + 1 + len(omitted_note) <= budget:
                kept.append(omitted_note)
            elif len(kept) == 1:
                return ""
            break
    return "\n".join(kept)


def format_project_index(
    index: ProjectIndex,
    *,
    active_path: str,
    root_path: str,
    max_macros: int = 20,
    max_chars: int = 6000,
) -> str:
    """Render a compact, budget-capped overview of the project index."""
    if max_chars <= 0:
        return ""

    norm_active = _normalize_rel_path(active_path)
    norm_root = _normalize_rel_path(root_path)
    sorted_paths = sorted(index.files)

    # 1. Files + line counts
    file_lines = ["## Files"]
    for rel_path in sorted_paths:
        fi = index.files[rel_path]
        tags: list[str] = []
        if rel_path == norm_root:
            tags.append("root")
        if rel_path == norm_active:
            tags.append("active")
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        file_lines.append(f"- {rel_path} ({fi.line_count} lines){tag_str}")
    files_text = "\n".join(file_lines)

    # 2. Hierarchical headings with file:start-end
    heading_lines = ["## Headings"]
    for rel_path in sorted_paths:
        for entity in index.files[rel_path].entities:
            if entity.kind != "heading":
                continue
            rank = _HEADING_RANK.get(entity.detail, 2)
            indent = "  " * rank
            heading_lines.append(
                f"{indent}- {entity.name} ({entity.file_path}:{entity.start_line}-{entity.end_line})"
            )

    # 3. Key labels and environments
    label_env_lines = ["## Labels & Environments"]
    for rel_path in sorted_paths:
        for entity in index.files[rel_path].entities:
            if entity.kind == "label":
                label_env_lines.append(
                    f"- \\label{{{entity.name}}} ({entity.file_path}:{entity.start_line}-{entity.end_line})"
                )
            elif entity.kind == "env":
                detail = f" [{entity.detail}]" if entity.detail else ""
                label_env_lines.append(
                    f"- {entity.name}{detail} ({entity.file_path}:{entity.start_line}-{entity.end_line})"
                )

    # 4. Custom macros (capped at max_macros)
    macros: list[IndexedEntity] = []
    for rel_path in sorted_paths:
        for entity in index.files[rel_path].entities:
            if entity.kind == "macro":
                macros.append(entity)

    macros_text = ""
    if macros:
        cap = max(max_macros, 0)
        macro_lines = ["## Custom Macros"]
        for macro in macros[:cap]:
            macro_lines.append(
                f"- {macro.name} ({macro.file_path}:{macro.start_line}-{macro.end_line})"
            )
        if len(macros) > cap:
            extra = len(macros) - cap
            macro_lines.append(f"(+{extra} more macros; search index to inspect)")
        macros_text = "\n".join(macro_lines)

    middle_lists = [lines for lines in (heading_lines, label_env_lines) if len(lines) > 1]
    full_sections = [files_text, *("\n".join(m) for m in middle_lists)]
    if macros_text:
        full_sections.append(macros_text)
    rendered = "\n\n".join(full_sections)
    if len(rendered) <= max_chars:
        return rendered

    # Budget middle sections while reserving space for ## Files and ## Custom Macros.
    reserved_len = len(files_text) + (2 + len(macros_text) if macros_text else 0)
    remaining = max_chars - reserved_len
    trimmed_middle: list[str] = []
    if remaining > 0 and middle_lists:
        if len(middle_lists) == 1:
            part = _trim_section_lines(middle_lists[0], remaining - 2)
            if part:
                trimmed_middle.append(part)
        else:
            avail = remaining - 4
            if avail > 0:
                h_full = "\n".join(middle_lists[0])
                l_full = "\n".join(middle_lists[1])
                if len(h_full) <= avail // 2:
                    h_budget = len(h_full)
                    l_budget = avail - h_budget
                elif len(l_full) <= avail // 2:
                    l_budget = len(l_full)
                    h_budget = avail - l_budget
                else:
                    h_budget = avail // 2
                    l_budget = avail - h_budget
                h_part = _trim_section_lines(middle_lists[0], h_budget)
                l_part = _trim_section_lines(middle_lists[1], l_budget)
                if h_part:
                    trimmed_middle.append(h_part)
                if l_part:
                    trimmed_middle.append(l_part)

    final_sections = [files_text, *trimmed_middle]
    if macros_text:
        final_sections.append(macros_text)
    rendered = "\n\n".join(final_sections)
    if len(rendered) <= max_chars:
        return rendered

    suffix = "\n... (truncated)"
    if max_chars <= len(suffix):
        return rendered[:max_chars]

    cutoff = max_chars - len(suffix)
    nl = rendered.rfind("\n", 0, cutoff)
    if nl > cutoff // 2:
        cutoff = nl
    return rendered[:cutoff].rstrip() + suffix
