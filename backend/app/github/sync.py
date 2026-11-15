from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.github.client import RemoteSnapshot
from app.github.errors import GithubApiError, GithubNotLinked, GithubSnapshotError, GithubSyncBlocked
from app.github.paths import MAX_FILE_BYTES, MAX_SNAPSHOT_BYTES, is_project_file
from app.github.store import GithubLink, GithubLinkStore
from app.projects.service import BLANK_MAIN_TEX, BLANK_REFERENCES_BIB, ProjectService

logger = logging.getLogger(__name__)

# Files every new project starts with. Unedited copies are safe to replace or drop on the first pull.
_STARTER_FILES = {
    "main.tex": BLANK_MAIN_TEX.encode("utf-8"),
    "references.bib": BLANK_REFERENCES_BIB.encode("utf-8"),
}

SnapshotFn = Callable[[Path, str], None]


@dataclass(frozen=True)
class SyncOutcome:
    commit_sha: str | None
    added: tuple[str, ...]
    updated: tuple[str, ...]
    deleted: tuple[str, ...]
    unchanged: bool
    snapshot_warning: str | None = None


class FolderClient(Protocol):
    async def fetch_snapshot(self, owner: str, repo: str, branch: str, subpath: str) -> RemoteSnapshot: ...

    async def commit_folder(
        self,
        owner: str,
        repo: str,
        branch: str,
        subpath: str,
        base: RemoteSnapshot,
        files: dict[str, bytes],
        message: str,
    ) -> str: ...


def _location(link: GithubLink) -> str:
    folder = f"/{link.subpath}" if link.subpath else ""
    return f"github.com/{link.owner}/{link.repo}{folder} ({link.branch})"


def clean_message(raw: str) -> str:
    line = " ".join(raw.split())
    if not line:
        line = "Update project files"
    return line[:200]


def _upload_pdf(rel: str) -> bool:
    """Uploaded PDFs are gitignored, so a pull cannot restore them."""
    path = Path(rel)
    return path.parent.as_posix() == "project-uploads" and path.suffix.lower() == ".pdf"


def _syncable(rel: str) -> bool:
    return is_project_file(rel) and not _upload_pdf(rel)


def _sync_snapshot(remote: RemoteSnapshot) -> RemoteSnapshot:
    files = {path: item for path, item in remote.files.items() if _syncable(path)}
    return RemoteSnapshot(commit_sha=remote.commit_sha, tree_sha=remote.tree_sha, files=files)


def _contents(remote: RemoteSnapshot) -> dict[str, bytes]:
    return {path: item.content for path, item in remote.files.items()}


def _pull_block(updated: tuple[str, ...], deleted: tuple[str, ...]) -> GithubSyncBlocked:
    paths = sorted(set(updated) | set(deleted))
    if deleted and updated:
        message = "Pull would replace local files and remove files that are not in the GitHub folder."
    elif deleted:
        message = "Pull would remove local files that are not in the GitHub folder."
    else:
        message = "Pull would replace local files with the GitHub copies."
    return GithubSyncBlocked("confirm_required", message, paths)


def _file_diff(
    current: dict[str, bytes],
    desired: dict[str, bytes],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    added: list[str] = []
    updated: list[str] = []
    for path in sorted(desired):
        if path not in current:
            added.append(path)
        elif current[path] != desired[path]:
            updated.append(path)
    deleted = tuple(sorted(path for path in current if path not in desired))
    return tuple(added), tuple(updated), deleted


class GithubSync:
    """Copy the files shown in a project to or from a GitHub folder.

    Local version history keeps using the project `.git` directory. This class
    never runs git and never attaches a remote. A caller can pass `snapshot`
    to record the working tree in that local history around a pull.
    """

    def __init__(self, project_service: ProjectService, store: GithubLinkStore, client: FolderClient) -> None:
        self.project_service = project_service
        self.store = store
        self.client = client

    def _require_link(self, project_id: str) -> GithubLink:
        link = self.store.get(project_id)
        if link is None:
            raise GithubNotLinked()
        return link

    def _local_files(self, project_id: str) -> dict[str, bytes]:
        files: dict[str, bytes] = {}
        total = 0
        for node in self.project_service.list_files(project_id):
            if not _syncable(node.path):
                continue
            loaded = self.project_service.read_file(project_id, node.path)
            if len(loaded.content) > MAX_FILE_BYTES:
                raise GithubApiError(413, f"{node.path} is larger than 20MB and was not synced.")
            total += len(loaded.content)
            if total > MAX_SNAPSHOT_BYTES:
                raise GithubApiError(413, "That project is larger than 100MB and was not synced.")
            files[node.path] = loaded.content
        return files

    def _locked_local_files(self, project_id: str) -> dict[str, bytes]:
        with self.project_service.file_lock(project_id):
            return self._local_files(project_id)

    def _rollback_pull(
        self,
        project_id: str,
        before: dict[str, bytes],
        added: tuple[str, ...],
        updated: tuple[str, ...],
        deleted: tuple[str, ...],
    ) -> None:
        for path in added:
            if path not in before:
                try:
                    self.project_service.delete_file(project_id, path)
                except FileNotFoundError:
                    pass
        for path in updated + deleted:
            if path in before:
                self.project_service.write_file(project_id, path, before[path])

    def _adopt_remote_root(self, project_id: str, project_dir: Path, remote_files: dict[str, bytes]) -> None:
        """If the main document was a dropped starter file, point it at the pulled document root."""
        meta = self.project_service.get_project(project_id)
        if (project_dir / meta.root_tex).is_file():
            return
        candidates = sorted(
            (p for p, content in remote_files.items() if p.lower().endswith(".tex") and b"\\documentclass" in content),
            key=lambda p: (p.count("/"), p != "main.tex", p),
        )
        if candidates:
            self.project_service.update_project_settings(project_id, root_tex=candidates[0])

    def _pull_locked(
        self,
        project_id: str,
        project_dir: Path,
        place: str,
        remote_files: dict[str, bytes],
        confirm_overwrite: bool,
        snapshot: SnapshotFn | None,
        first_pull: bool = False,
    ) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], str | None]:
        """Read, snapshot, and write while holding the project file lock.

        Later pulls mirror the GitHub folder. The first pull after linking merges instead:
        local-only files are kept (the next push adds them), and unedited starter files
        are replaced or dropped without asking. Only edited files GitHub would overwrite need confirming.
        """
        with self.project_service.file_lock(project_id):
            local = self._local_files(project_id)
            added, updated, deleted = _file_diff(local, remote_files)
            if first_pull:
                untouched = {p for p, content in local.items() if _STARTER_FILES.get(p) == content}
                deleted = tuple(p for p in deleted if p in untouched)
                needs_confirm = tuple(p for p in updated if p not in untouched)
            else:
                needs_confirm = updated
            if (needs_confirm or (deleted and not first_pull)) and not confirm_overwrite:
                raise _pull_block(needs_confirm, () if first_pull else deleted)
            if not added and not updated and not deleted:
                return "unchanged", (), (), (), None
            if snapshot is not None:
                snapshot(project_dir, f"Before pull from {place}")
            try:
                for path in added + updated:
                    self.project_service.write_file(project_id, path, remote_files[path])
                for path in deleted:
                    self.project_service.delete_file(project_id, path)
            except GithubSyncBlocked:
                raise
            except Exception:
                logger.exception("GitHub pull stopped while writing %s", project_id)
                try:
                    self._rollback_pull(project_id, local, added, updated, deleted)
                except Exception:
                    logger.exception("GitHub pull could not restore %s after a failed write", project_id)
                    raise GithubApiError(
                        500,
                        "The pull stopped after changing some files. Restore the snapshot taken before this pull.",
                    ) from None
                raise GithubApiError(500, "The pull stopped and the local files were put back.") from None
            if first_pull:
                self._adopt_remote_root(project_id, project_dir, remote_files)
            warning: str | None = None
            if snapshot is not None:
                try:
                    snapshot(project_dir, f"Pulled from {place}")
                except GithubSnapshotError:
                    logger.warning("Local snapshot after GitHub pull failed for %s", project_id, exc_info=True)
                    warning = "Files were pulled, but the local version snapshot could not be saved."
            return "done", added, updated, deleted, warning

    async def pull(
        self,
        project_id: str,
        *,
        confirm_overwrite: bool,
        snapshot: SnapshotFn | None = None,
        compile_lock: asyncio.Lock | None = None,
    ) -> SyncOutcome:
        link = self._require_link(project_id)
        remote = _sync_snapshot(
            await self.client.fetch_snapshot(link.owner, link.repo, link.branch, link.subpath)
        )
        if link.last_synced_sha is not None and remote.commit_sha == link.last_synced_sha and not confirm_overwrite:
            return SyncOutcome(remote.commit_sha, (), (), (), True)
        remote_files = _contents(remote)
        project_dir = self.project_service.get_project_dir(project_id)
        place = _location(link)
        lock = compile_lock if compile_lock is not None else asyncio.Lock()
        async with lock:
            status, added, updated, deleted, warning = await asyncio.to_thread(
                self._pull_locked,
                project_id,
                project_dir,
                place,
                remote_files,
                confirm_overwrite,
                snapshot,
                link.last_synced_sha is None,
            )
            if status == "unchanged":
                self.store.mark_synced(project_id, remote.commit_sha)
                return SyncOutcome(remote.commit_sha, (), (), (), True)
            self.store.mark_synced(project_id, remote.commit_sha)
        return SyncOutcome(remote.commit_sha, added, updated, deleted, False, warning)

    async def push(
        self,
        project_id: str,
        *,
        message: str,
        confirm_overwrite: bool,
        compile_lock: asyncio.Lock | None = None,
    ) -> SyncOutcome:
        link = self._require_link(project_id)
        remote = _sync_snapshot(
            await self.client.fetch_snapshot(link.owner, link.repo, link.branch, link.subpath)
        )
        if link.last_synced_sha and remote.commit_sha != link.last_synced_sha:
            raise GithubSyncBlocked(
                "remote_moved",
                "The GitHub branch moved since the last sync. Pull before pushing.",
                [],
            )
        lock = compile_lock if compile_lock is not None else asyncio.Lock()
        async with lock:
            local = await asyncio.to_thread(self._locked_local_files, project_id)
            added, updated, deleted = _file_diff(_contents(remote), local)
            if not added and not updated and not deleted:
                self.store.mark_synced(project_id, remote.commit_sha)
                return SyncOutcome(remote.commit_sha, (), (), (), True)
            if link.last_synced_sha is None and remote.files and not confirm_overwrite:
                raise GithubSyncBlocked(
                    "confirm_required",
                    "The GitHub folder already has files. Pull them first, or confirm to overwrite that folder.",
                    sorted(remote.files),
                )
            new_sha = await self.client.commit_folder(
                link.owner,
                link.repo,
                link.branch,
                link.subpath,
                remote,
                local,
                clean_message(message),
            )
            try:
                self.store.mark_synced(project_id, new_sha)
            except Exception as exc:
                logger.exception("GitHub push committed %s but the sync cursor was not saved", new_sha)
                raise GithubSyncBlocked(
                    "remote_moved",
                    f"Pushed commit {new_sha}, but the sync cursor was not saved. Pull before pushing again.",
                    [],
                ) from exc
        return SyncOutcome(new_sha, added, updated, deleted, False)
