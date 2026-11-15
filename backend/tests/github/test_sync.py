from __future__ import annotations

from pathlib import Path

import pytest

from app.config import bootstrap_data_dir
from app.db import DatabaseManager
from app.github.client import RemoteFile, RemoteSnapshot
from app.github.errors import GithubApiError, GithubSyncBlocked
from app.github.paths import normalize_branch, normalize_owner, normalize_repo, normalize_subpath
from app.github.store import GithubLinkStore
from app.github.sync import GithubSync
from app.projects.service import ProjectService

SHA_A = "a" * 40
SHA_B = "b" * 40
TREE_A = "c" * 40


def _services(tmp_path: Path) -> tuple[ProjectService, GithubLinkStore]:
    settings = bootstrap_data_dir(tmp_path / "data")
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()
    return ProjectService(data_dir=settings.data_dir, db=db), GithubLinkStore(db)


class FakeGitHub:
    def __init__(self, snapshot: RemoteSnapshot) -> None:
        self.snapshot = snapshot
        self.commits: list[dict] = []
        self.fetch: tuple[str, str, str, str] | None = None

    async def fetch_snapshot(self, owner: str, repo: str, branch: str, subpath: str) -> RemoteSnapshot:
        self.fetch = (owner, repo, branch, subpath)
        return self.snapshot

    async def commit_folder(
        self,
        owner: str,
        repo: str,
        branch: str,
        subpath: str,
        base: RemoteSnapshot,
        files: dict[str, bytes],
        message: str,
    ) -> str:
        self.commits.append(
            {
                "subpath": subpath,
                "base": base,
                "files": files,
                "message": message,
            }
        )
        return SHA_B


def _chapter_snapshot() -> RemoteSnapshot:
    content = b"\\section{From GitHub}\n"
    return RemoteSnapshot(
        commit_sha=SHA_A,
        tree_sha=TREE_A,
        files={
            "chapter.tex": RemoteFile("paper/chapter.tex", "chapter.tex", "d" * 40, content),
        },
    )


def test_path_rules_reject_traversal_and_hidden_folders() -> None:
    assert normalize_subpath(" paper/./chapters ") == "paper/chapters"
    assert normalize_subpath("") == ""
    assert normalize_branch("release/1.0") == "release/1.0"
    assert normalize_owner("a-b") == "a-b"
    assert normalize_repo("my.repo") == "my.repo"
    with pytest.raises(ValueError):
        normalize_subpath("../secret")
    with pytest.raises(ValueError):
        normalize_subpath("paper/.git")
    with pytest.raises(ValueError):
        normalize_branch("feature..x")
    with pytest.raises(ValueError):
        normalize_owner("-nope")
    with pytest.raises(ValueError):
        normalize_repo("..")


def test_connector_modules_do_not_call_git() -> None:
    import app.github.client as client_mod
    import app.github.sync as sync_mod
    import inspect

    for module in (client_mod, sync_mod):
        source = inspect.getsource(module)
        assert "subprocess" not in source
        assert "GitSnapshotService" not in source


@pytest.mark.asyncio
async def test_first_pull_into_a_new_project_replaces_unedited_starter_files(tmp_path: Path) -> None:
    projects, store = _services(tmp_path)
    project = projects.create_project("Manuscript")
    store.upsert(project.project_id, "acme", "manuscript", "main", "paper")
    fake = FakeGitHub(_chapter_snapshot())
    sync = GithubSync(projects, store, fake)
    notes: list[str] = []

    outcome = await sync.pull(
        project.project_id,
        confirm_overwrite=False,
        snapshot=lambda _path, message: notes.append(message),
    )
    project_dir = projects.get_project_dir(project.project_id)
    assert (project_dir / "chapter.tex").read_bytes() == b"\\section{From GitHub}\n"
    assert not (project_dir / "main.tex").exists()
    assert not (project_dir / "references.bib").exists()
    assert outcome.added == ("chapter.tex",)
    assert "main.tex" in outcome.deleted
    assert notes == [
        "Before pull from github.com/acme/manuscript/paper (main)",
        "Pulled from github.com/acme/manuscript/paper (main)",
    ]
    assert store.get(project.project_id).last_synced_sha == SHA_A
    assert fake.fetch == ("acme", "manuscript", "main", "paper")
    assert fake.commits == []


@pytest.mark.asyncio
async def test_push_sends_folder_files_and_blocks_a_moved_branch(tmp_path: Path) -> None:
    projects, store = _services(tmp_path)
    project = projects.create_project("Manuscript")
    store.upsert(project.project_id, "acme", "manuscript", "main", "paper")
    png = b"\x89PNG\r\n\x00\xff"
    snapshot = RemoteSnapshot(
        commit_sha=SHA_A,
        tree_sha=TREE_A,
        files={
            "chapter.tex": RemoteFile("paper/chapter.tex", "chapter.tex", "d" * 40, b"old"),
            "figures/plot.png": RemoteFile("paper/figures/plot.png", "figures/plot.png", "e" * 40, png),
        },
    )
    fake = FakeGitHub(snapshot)
    sync = GithubSync(projects, store, fake)

    with pytest.raises(GithubSyncBlocked) as blocked:
        await sync.push(project.project_id, message="Publish", confirm_overwrite=False)
    assert blocked.value.code == "confirm_required"
    assert fake.commits == []

    projects.write_file(project.project_id, "chapter.tex", b"new chapter")
    outcome = await sync.push(
        project.project_id,
        message="Publish chapter",
        confirm_overwrite=True,
    )
    assert outcome.updated == ("chapter.tex",)
    assert "main.tex" in outcome.added
    assert "references.bib" in outcome.added
    assert outcome.deleted == ("figures/plot.png",)
    sent = fake.commits[0]
    assert sent["subpath"] == "paper"
    assert sent["files"]["chapter.tex"] == b"new chapter"
    assert "figures/plot.png" not in sent["files"]
    assert sent["message"] == "Publish chapter"
    assert store.get(project.project_id).last_synced_sha == SHA_B

    fake.snapshot = RemoteSnapshot(SHA_A[:-1] + "b", TREE_A, snapshot.files)
    # The stored cursor is SHA_B, which no longer matches the branch.
    with pytest.raises(GithubSyncBlocked) as moved:
        await sync.push(project.project_id, message="Again", confirm_overwrite=True)
    assert moved.value.code == "remote_moved"
    assert len(fake.commits) == 1


@pytest.mark.asyncio
async def test_pull_asks_before_replacing_a_changed_local_file(tmp_path: Path) -> None:
    projects, store = _services(tmp_path)
    project = projects.create_project("Manuscript")
    store.upsert(project.project_id, "acme", "manuscript", "main", "")
    main = (projects.get_project_dir(project.project_id) / "main.tex").read_bytes()
    bib = (projects.get_project_dir(project.project_id) / "references.bib").read_bytes()
    projects.write_file(project.project_id, "chapter.tex", b"local chapter")
    snapshot = RemoteSnapshot(
        commit_sha=SHA_A,
        tree_sha=TREE_A,
        files={
            "main.tex": RemoteFile("main.tex", "main.tex", "d" * 40, main),
            "references.bib": RemoteFile("references.bib", "references.bib", "e" * 40, bib),
            "chapter.tex": RemoteFile("chapter.tex", "chapter.tex", "f" * 40, b"github chapter"),
        },
    )
    sync = GithubSync(projects, store, FakeGitHub(snapshot))
    with pytest.raises(GithubSyncBlocked) as blocked:
        await sync.pull(project.project_id, confirm_overwrite=False)
    assert blocked.value.code == "confirm_required"
    assert blocked.value.paths == ["chapter.tex"]
    assert (projects.get_project_dir(project.project_id) / "chapter.tex").read_bytes() == b"local chapter"


@pytest.mark.asyncio
async def test_sync_leaves_secret_and_upload_files_alone(tmp_path: Path) -> None:
    projects, store = _services(tmp_path)
    project = projects.create_project("Manuscript")
    store.upsert(project.project_id, "acme", "manuscript", "main", "paper")
    project_dir = projects.get_project_dir(project.project_id)
    (project_dir / ".ssh").mkdir()
    (project_dir / ".ssh" / "id_rsa").write_bytes(b"secret-key")
    (project_dir / "notes").write_bytes(b"no-extension")
    (project_dir / "project-uploads").mkdir()
    (project_dir / "project-uploads" / "scan.pdf").write_bytes(b"%PDF")
    fake = FakeGitHub(_chapter_snapshot())
    sync = GithubSync(projects, store, fake)
    await sync.pull(project.project_id, confirm_overwrite=True)
    assert (project_dir / ".ssh" / "id_rsa").read_bytes() == b"secret-key"
    assert (project_dir / "notes").read_bytes() == b"no-extension"
    assert (project_dir / "project-uploads" / "scan.pdf").read_bytes() == b"%PDF"

    projects.write_file(project.project_id, "chapter.tex", b"published chapter")
    await sync.push(project.project_id, message="Publish", confirm_overwrite=True)
    sent = fake.commits[0]["files"]
    assert sent["chapter.tex"] == b"published chapter"
    assert "notes" not in sent
    assert ".ssh/id_rsa" not in sent
    assert "project-uploads/scan.pdf" not in sent


@pytest.mark.asyncio
async def test_failed_pull_puts_local_files_back(tmp_path: Path) -> None:
    projects, store = _services(tmp_path)
    project = projects.create_project("Manuscript")
    store.upsert(project.project_id, "acme", "manuscript", "main", "paper")
    original = (projects.get_project_dir(project.project_id) / "main.tex").read_bytes()
    real_delete = projects.delete_file
    calls = {"n": 0}

    def fail_once(project_id: str, rel_path: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        real_delete(project_id, rel_path)

    projects.delete_file = fail_once  # type: ignore[method-assign]
    sync = GithubSync(projects, store, FakeGitHub(_chapter_snapshot()))
    with pytest.raises(GithubApiError, match="put back"):
        await sync.pull(project.project_id, confirm_overwrite=True)
    project_dir = projects.get_project_dir(project.project_id)
    assert (project_dir / "main.tex").read_bytes() == original
    assert not (project_dir / "chapter.tex").exists()
    projects.delete_file = real_delete  # type: ignore[method-assign]


def test_delete_file_rejects_a_missing_path_and_a_directory(tmp_path: Path) -> None:
    projects, _store = _services(tmp_path)
    project = projects.create_project("Manuscript")
    with pytest.raises(FileNotFoundError):
        projects.delete_file(project.project_id, "gone.tex")
    project_dir = projects.get_project_dir(project.project_id)
    (project_dir / "main.tex").unlink()
    (project_dir / "main.tex").mkdir()
    with pytest.raises(PermissionError):
        projects.delete_file(project.project_id, "main.tex")


def test_changing_the_linked_folder_clears_the_sync_cursor(tmp_path: Path) -> None:
    projects, store = _services(tmp_path)
    project = projects.create_project("Manuscript")
    store.upsert(project.project_id, "acme", "manuscript", "main", "paper")
    store.mark_synced(project.project_id, SHA_A)
    same = store.upsert(project.project_id, "acme", "manuscript", "main", "paper")
    assert same.last_synced_sha == SHA_A
    moved = store.upsert(project.project_id, "acme", "manuscript", "main", "notes")
    assert moved.last_synced_sha is None


def _snapshot(sha: str, files: dict[str, bytes]) -> RemoteSnapshot:
    return RemoteSnapshot(
        commit_sha=sha,
        tree_sha=TREE_A,
        files={p: RemoteFile(f"paper/{p}", p.split("/")[-1], "e" * 40, c) for p, c in files.items()},
    )


@pytest.mark.asyncio
async def test_first_pull_keeps_local_only_files_and_asks_only_for_edited_ones(tmp_path: Path) -> None:
    projects, store = _services(tmp_path)
    project = projects.create_project("Merge")
    pid = project.project_id
    projects.write_file(pid, "main.tex", "% my own edits\n")
    projects.write_file(pid, "notes.tex", "local notes\n")
    store.upsert(pid, "acme", "manuscript", "main", "paper")
    remote = {"main.tex": b"\\documentclass{article}\n% from GitHub\n", "intro.tex": b"intro\n"}
    sync = GithubSync(projects, store, FakeGitHub(_snapshot(SHA_A, remote)))

    with pytest.raises(GithubSyncBlocked) as blocked:
        await sync.pull(pid, confirm_overwrite=False)
    assert blocked.value.paths == ["main.tex"]

    outcome = await sync.pull(pid, confirm_overwrite=True)
    project_dir = projects.get_project_dir(pid)
    assert (project_dir / "main.tex").read_bytes() == remote["main.tex"]
    assert (project_dir / "notes.tex").read_text() == "local notes\n"
    assert not (project_dir / "references.bib").exists()  # unedited starter, not in the repo
    assert outcome.deleted == ("references.bib",)

    # Later pulls mirror GitHub again: a local file missing there needs confirming.
    sync.client = FakeGitHub(_snapshot(SHA_B, remote))
    with pytest.raises(GithubSyncBlocked) as later:
        await sync.pull(pid, confirm_overwrite=False)
    assert "notes.tex" in later.value.paths


@pytest.mark.asyncio
async def test_first_pull_switches_main_document_to_the_repository_root(tmp_path: Path) -> None:
    projects, store = _services(tmp_path)
    project = projects.create_project("Root")
    store.upsert(project.project_id, "acme", "manuscript", "main", "paper")
    remote = {
        "manuscript.tex": b"\\documentclass{book}\n\\begin{document}\\end{document}\n",
        "chapters/one.tex": b"\\chapter{One}\n",
    }
    sync = GithubSync(projects, store, FakeGitHub(_snapshot(SHA_A, remote)))
    await sync.pull(project.project_id, confirm_overwrite=False)
    assert projects.get_project(project.project_id).root_tex == "manuscript.tex"


@pytest.mark.asyncio
async def test_pull_then_push_when_remote_unchanged_preserves_and_pushes_local_edits(tmp_path: Path) -> None:
    projects, store = _services(tmp_path)
    project = projects.create_project("SyncFlow")
    pid = project.project_id
    store.upsert(pid, "acme", "manuscript", "main", "paper")
    remote = {"main.tex": b"\\documentclass{article}\n\\begin{document}v1\\end{document}\n"}
    fake = FakeGitHub(_snapshot(SHA_A, remote))
    sync = GithubSync(projects, store, fake)

    first = await sync.pull(pid, confirm_overwrite=False)
    assert not first.unchanged
    assert store.get(pid).last_synced_sha == SHA_A

    # Local edit + new file while remote is still at SHA_A
    projects.write_file(pid, "main.tex", b"\\documentclass{article}\n\\begin{document}v2\\end{document}\n")
    projects.write_file(pid, "extra.tex", b"\\section{Extra}\n")

    pulled = await sync.pull(pid, confirm_overwrite=False)
    assert pulled.unchanged is True
    pushed = await sync.push(pid, message="Update project files", confirm_overwrite=False)
    assert pushed.unchanged is False
    assert pushed.added == ("extra.tex",)
    assert pushed.updated == ("main.tex",)
    assert store.get(pid).last_synced_sha == SHA_B

