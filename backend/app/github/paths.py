from __future__ import annotations

import re
from pathlib import Path

from app.projects.service import ALLOWED_EXTENSIONS, EXCLUDED_DIRS

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024


def normalize_owner(raw: str) -> str:
    owner = raw.strip()
    if "--" in owner or not _OWNER_RE.fullmatch(owner):
        raise ValueError("GitHub owner must be 1–39 letters, numbers, or hyphens.")
    return owner


def normalize_repo(raw: str) -> str:
    repo = raw.strip()
    if repo in {".", ".."} or not _REPO_RE.fullmatch(repo):
        raise ValueError("GitHub repository name is not valid.")
    return repo


def normalize_branch(raw: str) -> str:
    branch = raw.strip()
    if (
        not branch
        or len(branch) > 255
        or branch.startswith("/")
        or branch.endswith("/")
        or branch.endswith(".lock")
        or branch.startswith("-")
        or ".." in branch
        or "@{" in branch
        or "//" in branch
    ):
        raise ValueError("GitHub branch name is not valid.")
    for char in branch:
        if char.isspace() or char in "~^:?*[\\" or ord(char) < 32:
            raise ValueError("GitHub branch name is not valid.")
    return branch


def normalize_subpath(raw: str) -> str:
    """Folder inside the repo. Empty means the repository root."""
    text = raw.strip().replace("\\", "/").strip("/")
    if not text:
        return ""
    parts: list[str] = []
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == ".." or part.startswith(".") or part in EXCLUDED_DIRS or "\x00" in part:
            raise ValueError("GitHub folder path cannot contain '..' or hidden folders.")
        parts.append(part)
    joined = "/".join(parts)
    if len(joined) > 500:
        raise ValueError("GitHub folder path is too long.")
    return joined


def normalize_sha(raw: str | None) -> str | None:
    if raw is None:
        return None
    sha = raw.strip().lower()
    if not sha:
        return None
    if not _SHA_RE.fullmatch(sha):
        raise ValueError("GitHub commit id is not valid.")
    return sha


def is_project_file(rel: str) -> bool:
    if not rel or "\x00" in rel or "\\" in rel or rel.startswith("/"):
        return False
    path = Path(rel)
    if path.is_absolute():
        return False
    parts = path.parts
    if not parts:
        return False
    for part in parts:
        if part in {"", ".", ".."} or part.startswith(".") or part in EXCLUDED_DIRS:
            return False
    ext = path.suffix.lower()
    return bool(ext) and ext in ALLOWED_EXTENSIONS


def project_path_from_repo(subpath: str, repo_path: str) -> str | None:
    cleaned = repo_path.strip().replace("\\", "/").strip("/")
    if not cleaned:
        return None
    if subpath:
        prefix = subpath + "/"
        if not cleaned.startswith(prefix):
            return None
        rel = cleaned[len(prefix) :]
    else:
        rel = cleaned
    if not is_project_file(rel):
        return None
    return rel


def repo_path_for(subpath: str, project_path: str) -> str:
    if subpath:
        return f"{subpath}/{project_path}"
    return project_path
