from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator
import secrets as pysecrets

from app.db import DatabaseManager

EXCLUDED_DIRS = frozenset({".git", ".build", ".texmf", "paper-reviews", "__pycache__"})
ALLOWED_EXTENSIONS = frozenset(
    {
        ".tex",
        ".bib",
        ".cls",
        ".sty",
        ".bst",
        ".cfg",
        ".def",
        ".md",
        ".txt",
        ".csv",
        ".png",
        ".jpg",
        ".jpeg",
        ".svg",
        ".eps",
        ".pdf",
    }
)

BLANK_MAIN_TEX = """\\documentclass{article}
\\usepackage[utf8]{inputenc}
\\title{Untitled Document}
\\author{Author}
\\date{\\today}

\\begin{document}
\\maketitle

\\section{Introduction}
Write your manuscript here.

\\bibliographystyle{plain}
\\bibliography{references}
\\end{document}
"""

BLANK_REFERENCES_BIB = """% BibTeX references for this project
"""

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ETagConflictError(Exception):
    """Raised when an optimistic concurrency ETag check fails during file write."""

    def __init__(self, current_etag: str, server_content: str) -> None:
        super().__init__(f"ETag conflict: current server ETag is {current_etag}")
        self.current_etag = current_etag
        self.server_content = server_content


@dataclass(frozen=True)
class ProjectMetadata:
    project_id: str
    name: str
    root_tex: str
    zotero_collection_key: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class FileNode:
    path: str
    name: str
    is_dir: bool
    size_bytes: int
    etag: str | None = None


@dataclass(frozen=True)
class FileContentWithETag:
    path: str
    content: bytes
    text: str
    etag: str


@dataclass(frozen=True)
class FileWriteResult:
    path: str
    etag: str
    size_bytes: int


@dataclass(frozen=True)
class FileChangeEvent:
    change_type: str  # "added" | "modified" | "deleted"
    path: str
    etag: str | None = None


def _slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "project"


def _compute_etag(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_UNSET: Any = object()


class ProjectService:
    """Manages project directories, metadata in SQLite, ETag file writes, and disk watching."""

    def __init__(self, data_dir: Path, db: DatabaseManager) -> None:
        self.data_dir = Path(data_dir)
        self.projects_root = self.data_dir / "projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.db = db
        self._file_locks_guard = threading.Lock()
        self._file_locks: dict[str, threading.RLock] = {}

    def file_lock(self, project_id: str) -> threading.RLock:
        with self._file_locks_guard:
            lock = self._file_locks.get(project_id)
            if lock is None:
                lock = threading.RLock()
                self._file_locks[project_id] = lock
            return lock

    def get_project_dir(self, project_id: str) -> Path:
        if not project_id or "/" in project_id or "\\" in project_id or ".." in project_id:
            raise PermissionError(f"Invalid project_id: {project_id!r}")
        root_resolved = self.projects_root.resolve()
        candidate = (self.projects_root / project_id).resolve()
        if not candidate.is_relative_to(root_resolved) or candidate == root_resolved:
            raise PermissionError(f"Project path escapes projects root: {project_id!r}")
        return candidate

    def _validate_project_rel_path(self, project_dir: Path, rel_path: str) -> Path:
        cleaned = rel_path.strip().lstrip("/")
        if not cleaned:
            raise PermissionError("Empty relative file path is not allowed")

        rel_obj = Path(cleaned)
        if rel_obj.is_absolute():
            raise PermissionError("Absolute paths are not allowed")

        # Reject any path component targeting excluded/sensitive directories (e.g., .git, .texmf, .build)
        for part in rel_obj.parts:
            if part in {"", "."}:
                continue
            if part == "..":
                raise PermissionError(f"Path traversal '..' is not allowed: {rel_path!r}")
            if part in EXCLUDED_DIRS or part in {".git", ".texmf"}:
                raise PermissionError(f"Access to internal directory '{part}' is forbidden")

        project_root_resolved = project_dir.resolve()
        raw_target = project_dir / rel_obj

        # Check every ancestor up to project_dir for symlinks
        current = raw_target
        while current != project_dir and current != current.parent:
            if current.is_symlink():
                raise PermissionError(f"Symlinks are not allowed inside project tree: {rel_path!r}")
            current = current.parent

        resolved_target = raw_target.resolve()
        if not resolved_target.is_relative_to(project_root_resolved):
            raise PermissionError(f"Resolved path escapes project root: {rel_path!r}")

        return resolved_target

    def create_project(self, name: str, template: str = "blank") -> ProjectMetadata:
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Project name cannot be empty")

        base_slug = _slugify(clean_name)
        slug = base_slug
        suffix = 2
        while (self.projects_root / slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1

        project_dir = self.get_project_dir(slug)
        project_dir.mkdir(parents=True, exist_ok=False)

        root_tex = "main.tex"
        (project_dir / root_tex).write_text(BLANK_MAIN_TEX, encoding="utf-8")
        (project_dir / "references.bib").write_text(BLANK_REFERENCES_BIB, encoding="utf-8")

        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(self.db.get_connection()) as conn:
            conn.execute(
                """
                INSERT INTO projects (project_id, name, root_tex, zotero_collection_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (slug, clean_name, root_tex, None, now_iso, now_iso),
            )

        return ProjectMetadata(
            project_id=slug,
            name=clean_name,
            root_tex=root_tex,
            zotero_collection_key=None,
            created_at=now_iso,
            updated_at=now_iso,
        )

    def list_projects(self) -> list[ProjectMetadata]:
        with closing(self.db.get_connection()) as conn:
            rows = conn.execute(
                """
                SELECT project_id, name, root_tex, zotero_collection_key, created_at, updated_at
                FROM projects
                ORDER BY updated_at DESC, created_at DESC;
                """
            ).fetchall()
        return [
            ProjectMetadata(
                project_id=str(r["project_id"]),
                name=str(r["name"]),
                root_tex=str(r["root_tex"] or "main.tex"),
                zotero_collection_key=r["zotero_collection_key"],
                created_at=str(r["created_at"]),
                updated_at=str(r["updated_at"]),
            )
            for r in rows
        ]

    def get_project(self, project_id: str) -> ProjectMetadata:
        with closing(self.db.get_connection()) as conn:
            row = conn.execute(
                """
                SELECT project_id, name, root_tex, zotero_collection_key, created_at, updated_at
                FROM projects
                WHERE project_id = ?;
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Project not found: {project_id}")
        return ProjectMetadata(
            project_id=str(row["project_id"]),
            name=str(row["name"]),
            root_tex=str(row["root_tex"] or "main.tex"),
            zotero_collection_key=row["zotero_collection_key"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def update_project_settings(
        self,
        project_id: str,
        root_tex: str | None = None,
        zotero_collection_key: str | None | object = _UNSET,
    ) -> ProjectMetadata:
        current = self.get_project(project_id)
        project_dir = self.get_project_dir(project_id)

        new_root_tex = current.root_tex
        if root_tex is not None:
            validated = self._validate_project_rel_path(project_dir, root_tex)
            new_root_tex = validated.relative_to(project_dir.resolve()).as_posix()

        if zotero_collection_key is _UNSET:
            new_zotero_key = current.zotero_collection_key
        elif zotero_collection_key is None or str(zotero_collection_key).strip() == "":
            new_zotero_key = None
        else:
            new_zotero_key = str(zotero_collection_key).strip()
        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(self.db.get_connection()) as conn:
            conn.execute(
                """
                UPDATE projects
                SET root_tex = ?, zotero_collection_key = ?, updated_at = ?
                WHERE project_id = ?;
                """,
                (new_root_tex, new_zotero_key, now_iso, project_id),
            )
        return self.get_project(project_id)

    def rename_project(self, project_id: str, name: str) -> ProjectMetadata:
        """Change the display name; the project_id (folder slug) stays the same."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Project name cannot be empty")
        self.get_project(project_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        with closing(self.db.get_connection()) as conn:
            conn.execute(
                "UPDATE projects SET name = ?, updated_at = ? WHERE project_id = ?;",
                (clean_name, now_iso, project_id),
            )
        return self.get_project(project_id)

    def duplicate_project(self, project_id: str, name: str | None = None) -> ProjectMetadata:
        """Copy the project's files into a new project.

        Version history (.git), build output and the GitHub link are not copied: the copy starts fresh.
        """
        source = self.get_project(project_id)
        source_dir = self.get_project_dir(project_id)
        clean_name = (name or "").strip() or f"{source.name} (copy)"

        base_slug = _slugify(clean_name)
        slug = base_slug
        suffix = 2
        while (self.projects_root / slug).exists():
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        target_dir = self.get_project_dir(slug)

        if source_dir.is_dir():
            shutil.copytree(
                source_dir,
                target_dir,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git", ".build"),
            )
        else:
            target_dir.mkdir(parents=True)

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with closing(self.db.get_connection()) as conn:
                conn.execute(
                    """
                    INSERT INTO projects (project_id, name, root_tex, zotero_collection_key, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (slug, clean_name, source.root_tex, source.zotero_collection_key, now_iso, now_iso),
                )
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise
        return self.get_project(slug)

    def delete_project(self, project_id: str) -> None:
        """Remove the project's database rows and its folder on disk."""
        self.get_project(project_id)
        project_dir = self.get_project_dir(project_id)
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                # These tables have no ON DELETE CASCADE from projects.
                for table in ("chat_messages", "pdf_chunks", "pdf_documents", "candidate_sources"):
                    conn.execute(f"DELETE FROM {table} WHERE project_id = ?;", (project_id,))
                conn.execute("DELETE FROM projects WHERE project_id = ?;", (project_id,))
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        if project_dir.is_dir():
            shutil.rmtree(project_dir)
        attachments_dir = self.data_dir / "chat-attachments" / project_dir.name
        if attachments_dir.is_dir():
            shutil.rmtree(attachments_dir)
        with self._file_locks_guard:
            self._file_locks.pop(project_id, None)

    def list_files(self, project_id: str) -> list[FileNode]:
        self.get_project(project_id)
        project_dir = self.get_project_dir(project_id)
        if not project_dir.is_dir():
            return []

        root_resolved = project_dir.resolve()
        nodes: list[FileNode] = []

        for dirpath, dirnames, filenames in os.walk(root_resolved, followlinks=False):
            # Filter out excluded directories in-place so os.walk does not descend into them
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in EXCLUDED_DIRS
                and not d.startswith(".git")
                and not (Path(dirpath) / d).is_symlink()
            )
            current_dir = Path(dirpath)
            for fname in sorted(filenames):
                if fname.startswith("."):
                    continue
                fpath = current_dir / fname
                if fpath.is_symlink():
                    continue
                try:
                    resolved = fpath.resolve()
                    if not resolved.is_relative_to(root_resolved):
                        continue
                    rel_posix = resolved.relative_to(root_resolved).as_posix()
                except ValueError:
                    continue

                ext = resolved.suffix.lower()
                if ext and ext not in ALLOWED_EXTENSIONS:
                    continue

                data = resolved.read_bytes()
                nodes.append(
                    FileNode(
                        path=rel_posix,
                        name=fname,
                        is_dir=False,
                        size_bytes=len(data),
                        etag=_compute_etag(data),
                    )
                )

        return sorted(nodes, key=lambda n: n.path)

    def read_file(self, project_id: str, rel_path: str) -> FileContentWithETag:
        self.get_project(project_id)
        project_dir = self.get_project_dir(project_id)
        target = self._validate_project_rel_path(project_dir, rel_path)
        if not target.is_file():
            raise FileNotFoundError(f"File not found: {rel_path}")
        raw_bytes = target.read_bytes()
        etag = _compute_etag(raw_bytes)
        text = raw_bytes.decode("utf-8", errors="replace")
        rel_posix = target.relative_to(project_dir.resolve()).as_posix()
        return FileContentWithETag(
            path=rel_posix,
            content=raw_bytes,
            text=text,
            etag=etag,
        )

    def write_file(
        self,
        project_id: str,
        rel_path: str,
        content: bytes | str,
        expected_etag: str | None = None,
    ) -> FileWriteResult:
        with self.file_lock(project_id):
            return self._write_file(project_id, rel_path, content, expected_etag)

    def _write_file(
        self,
        project_id: str,
        rel_path: str,
        content: bytes | str,
        expected_etag: str | None,
    ) -> FileWriteResult:
        self.get_project(project_id)
        project_dir = self.get_project_dir(project_id)
        target = self._validate_project_rel_path(project_dir, rel_path)

        raw_bytes = content.encode("utf-8") if isinstance(content, str) else bytes(content)

        if target.exists():
            current_bytes = target.read_bytes()
            current_etag = _compute_etag(current_bytes)
            if expected_etag is not None and expected_etag != current_etag:
                server_text = current_bytes.decode("utf-8", errors="replace")
                raise ETagConflictError(
                    current_etag=current_etag,
                    server_content=server_text,
                )
        elif expected_etag is not None:
            raise ETagConflictError(current_etag="", server_content="")

        target.parent.mkdir(parents=True, exist_ok=True)
        nonce = pysecrets.token_hex(8)
        tmp_target = target.with_name(f".{target.name}.{nonce}.tmp")
        try:
            tmp_target.write_bytes(raw_bytes)
            tmp_target.replace(target)
        finally:
            if tmp_target.exists():
                try:
                    tmp_target.unlink()
                except OSError:
                    pass

        new_etag = _compute_etag(raw_bytes)
        rel_posix = target.relative_to(project_dir.resolve()).as_posix()
        return FileWriteResult(
            path=rel_posix,
            etag=new_etag,
            size_bytes=len(raw_bytes),
        )

    def delete_file(self, project_id: str, rel_path: str) -> None:
        with self.file_lock(project_id):
            self.get_project(project_id)
            project_dir = self.get_project_dir(project_id)
            target = self._validate_project_rel_path(project_dir, rel_path)
            ext = target.suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                raise PermissionError(f"File extension '{ext}' is not permitted")
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise PermissionError("Only files can be deleted")
            if not target.is_file():
                raise FileNotFoundError(f"File not found: {rel_path}")
            try:
                target.unlink()
            except FileNotFoundError as exc:
                raise FileNotFoundError(f"File not found: {rel_path}") from exc

    def list_folders(self, project_id: str) -> list[str]:
        """Project-relative folder paths, including empty ones, with the same exclusions as list_files."""
        self.get_project(project_id)
        project_dir = self.get_project_dir(project_id)
        if not project_dir.is_dir():
            return []
        root_resolved = project_dir.resolve()
        folders: list[str] = []
        for dirpath, dirnames, _filenames in os.walk(root_resolved, followlinks=False):
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in EXCLUDED_DIRS
                and not d.startswith(".")
                and not (Path(dirpath) / d).is_symlink()
            )
            for d in dirnames:
                folders.append((Path(dirpath) / d).relative_to(root_resolved).as_posix())
        return sorted(folders)

    def create_folder(self, project_id: str, rel_path: str) -> str:
        with self.file_lock(project_id):
            self.get_project(project_id)
            project_dir = self.get_project_dir(project_id)
            target = self._validate_project_rel_path(project_dir, rel_path)
            if any(part.startswith(".") for part in Path(rel_path.strip().strip("/")).parts):
                raise PermissionError("Folder names cannot start with '.'")
            if target.exists():
                raise FileExistsError(f"'{rel_path}' already exists")
            target.mkdir(parents=True)
            return target.relative_to(project_dir.resolve()).as_posix()

    def delete_folder(self, project_id: str, rel_path: str) -> None:
        """Delete a folder and everything in it."""
        with self.file_lock(project_id):
            self.get_project(project_id)
            project_dir = self.get_project_dir(project_id)
            target = self._validate_project_rel_path(project_dir, rel_path)
            if target.is_symlink() or not target.is_dir():
                raise FileNotFoundError(f"Folder not found: {rel_path}")
            if target.resolve() == project_dir.resolve():
                raise PermissionError("The project root cannot be deleted")
            shutil.rmtree(target)

    def move_path(self, project_id: str, from_path: str, to_path: str) -> tuple[str, ProjectMetadata]:
        """Rename or move a file or folder inside the project.

        If the project's root .tex file moves, root_tex follows it.
        """
        with self.file_lock(project_id):
            meta = self.get_project(project_id)
            project_dir = self.get_project_dir(project_id)
            root = project_dir.resolve()
            source = self._validate_project_rel_path(project_dir, from_path)
            target = self._validate_project_rel_path(project_dir, to_path)
            if source.is_symlink() or not source.exists():
                raise FileNotFoundError(f"Not found: {from_path}")
            if target.exists():
                raise FileExistsError(f"'{to_path}' already exists")
            if source.is_file():
                ext = target.suffix.lower()
                if ext and ext not in ALLOWED_EXTENSIONS:
                    raise PermissionError(f"File extension '{ext}' is not permitted")
            elif target.resolve().is_relative_to(source.resolve()):
                raise PermissionError("A folder cannot be moved into itself")
            target.parent.mkdir(parents=True, exist_ok=True)
            source.rename(target)

            old_rel = source.relative_to(root).as_posix()
            new_rel = target.relative_to(root).as_posix()
            root_tex = meta.root_tex
            if root_tex == old_rel or root_tex.startswith(f"{old_rel}/"):
                new_root = new_rel + root_tex[len(old_rel):]
                now_iso = datetime.now(timezone.utc).isoformat()
                with closing(self.db.get_connection()) as conn:
                    conn.execute(
                        "UPDATE projects SET root_tex = ?, updated_at = ? WHERE project_id = ?;",
                        (new_root, now_iso, project_id),
                    )
            return new_rel, self.get_project(project_id)

    def save_upload(self, project_id: str, filename: str, content: bytes, folder: str = "") -> FileWriteResult:
        """Save an upload at the project root, or inside `folder` (project-relative)."""
        clean_name = Path(filename).name.strip()
        if not clean_name or clean_name in {".", ".."} or clean_name.startswith("."):
            raise PermissionError(f"Invalid upload filename: {filename!r}")
        ext = Path(clean_name).suffix.lower()
        if ext and ext not in ALLOWED_EXTENSIONS:
            raise PermissionError(f"File extension '{ext}' is not permitted for upload")
        clean_folder = folder.strip().strip("/")
        rel_path = f"{clean_folder}/{clean_name}" if clean_folder else clean_name
        return self.write_file(
            project_id=project_id,
            rel_path=rel_path,
            content=content,
            expected_etag=None,
        )

    async def watch_project_files(self, project_id: str) -> AsyncIterator[FileChangeEvent]:
        self.get_project(project_id)
        project_dir = self.get_project_dir(project_id).resolve()

        try:
            from watchfiles import Change, awatch

            async for changes in awatch(project_dir):
                for change, raw_path in changes:
                    fpath = Path(raw_path)
                    try:
                        rel_posix = fpath.resolve().relative_to(project_dir).as_posix()
                    except ValueError:
                        continue
                    if any(part in EXCLUDED_DIRS or part.startswith(".") for part in Path(rel_posix).parts):
                        continue
                    if change == Change.added:
                        change_str = "added"
                    elif change == Change.deleted:
                        change_str = "deleted"
                    else:
                        change_str = "modified"
                    etag = None
                    if fpath.is_file():
                        try:
                            etag = _compute_etag(fpath.read_bytes())
                        except OSError:
                            pass
                    yield FileChangeEvent(change_type=change_str, path=rel_posix, etag=etag)
        except ImportError:
            # Polling fallback if watchfiles is not installed in the host environment
            snapshots: dict[str, str] = {
                node.path: (node.etag or "") for node in self.list_files(project_id)
            }
            while True:
                await asyncio.sleep(0.5)
                current_nodes = {
                    node.path: (node.etag or "") for node in self.list_files(project_id)
                }
                for path, etag in current_nodes.items():
                    if path not in snapshots:
                        yield FileChangeEvent(change_type="added", path=path, etag=etag)
                    elif snapshots[path] != etag:
                        yield FileChangeEvent(change_type="modified", path=path, etag=etag)
                for path in set(snapshots) - set(current_nodes):
                    yield FileChangeEvent(change_type="deleted", path=path, etag=None)
                snapshots = current_nodes
