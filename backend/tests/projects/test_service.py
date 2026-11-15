from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from app.config import bootstrap_data_dir
from app.db import DatabaseManager
from app.projects.service import ETagConflictError, ProjectService


def _make_service(tmp_path: Path) -> ProjectService:
    data_dir = tmp_path / "data"
    settings = bootstrap_data_dir(data_dir)
    db = DatabaseManager(db_path=settings.db_path, backup_dir=settings.backups_dir)
    db.initialize()
    return ProjectService(data_dir=settings.data_dir, db=db)


def test_create_and_list_project_files(tmp_path: Path) -> None:
    service = _make_service(tmp_path)

    meta = service.create_project(name="Quantum Entanglement Paper", template="blank")
    assert meta.project_id == "quantum-entanglement-paper"
    assert meta.name == "Quantum Entanglement Paper"
    assert meta.root_tex == "main.tex"

    projects = service.list_projects()
    assert len(projects) == 1
    assert projects[0].project_id == meta.project_id

    project_dir = service.get_project_dir(meta.project_id)
    assert (project_dir / "main.tex").is_file()
    assert (project_dir / "references.bib").is_file()
    assert not (project_dir / "project-uploads").exists()

    # Hidden internal dirs (.git, .build, .texmf) must be excluded from list_files
    (project_dir / ".git").mkdir()
    (project_dir / ".git" / "config").write_text("[core]", encoding="utf-8")
    (project_dir / ".build").mkdir()
    (project_dir / ".build" / "main.log").write_text("log", encoding="utf-8")

    files = service.list_files(meta.project_id)
    file_paths = {f.path for f in files}
    assert "main.tex" in file_paths
    assert "references.bib" in file_paths
    assert not any(p.startswith(".git") or p.startswith(".build") for p in file_paths)


def test_write_file_with_stale_etag_raises_conflict(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    meta = service.create_project(name="ETag Study")

    initial = service.read_file(meta.project_id, "main.tex")
    assert initial.etag
    assert r"\documentclass" in initial.text

    # First write with valid expected_etag succeeds and updates etag
    write_1 = service.write_file(
        project_id=meta.project_id,
        rel_path="main.tex",
        content=b"\\documentclass{article}\n\\begin{document}\nVersion 1\n\\end{document}\n",
        expected_etag=initial.etag,
    )
    assert write_1.etag != initial.etag

    # Second write using the now-stale initial.etag must raise ETagConflictError
    with pytest.raises(ETagConflictError) as exc_info:
        service.write_file(
            project_id=meta.project_id,
            rel_path="main.tex",
            content=b"\\documentclass{article}\n\\begin{document}\nConflicting Version\n\\end{document}\n",
            expected_etag=initial.etag,
        )

    assert exc_info.value.current_etag == write_1.etag
    assert "Version 1" in exc_info.value.server_content


def test_path_traversal_and_external_symlinks_blocked(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    meta = service.create_project(name="Security Test")
    project_dir = service.get_project_dir(meta.project_id)

    # 1. Path traversal outside project root
    with pytest.raises(PermissionError):
        service.read_file(meta.project_id, "../../config/settings.yaml")

    with pytest.raises(PermissionError):
        service.write_file(
            meta.project_id,
            "../../config/evil.yaml",
            b"evil: true",
            expected_etag=None,
        )

    # 2. Reading or writing inside .git/ directory is blocked
    (project_dir / ".git").mkdir(exist_ok=True)
    with pytest.raises(PermissionError):
        service.write_file(
            meta.project_id,
            ".git/config",
            b"[core]\nsshCommand = evil",
            expected_etag=None,
        )
    with pytest.raises(PermissionError):
        service.read_file(meta.project_id, ".git/config")

    # 3. Reading or writing through an external symlink is blocked
    outside_file = tmp_path / "outside_secret.txt"
    outside_file.write_text("TOP_SECRET", encoding="utf-8")
    (project_dir / "symlinked.tex").symlink_to(outside_file)

    with pytest.raises(PermissionError):
        service.read_file(meta.project_id, "symlinked.tex")

    with pytest.raises(PermissionError):
        service.write_file(
            meta.project_id,
            "symlinked.tex",
            b"overwritten",
            expected_etag=None,
        )


def test_rename_keeps_project_id_and_rejects_empty_name(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    meta = service.create_project(name="Draft Paper")

    renamed = service.rename_project(meta.project_id, "  Final Paper  ")
    assert renamed.project_id == meta.project_id
    assert renamed.name == "Final Paper"

    with pytest.raises(ValueError):
        service.rename_project(meta.project_id, "   ")
    with pytest.raises(KeyError):
        service.rename_project("missing", "Anything")


def test_duplicate_copies_files_but_not_history_or_build(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    meta = service.create_project(name="Original")
    source_dir = service.get_project_dir(meta.project_id)
    (source_dir / "chapters").mkdir()
    (source_dir / "chapters" / "ch1.tex").write_text("Chapter one", encoding="utf-8")
    (source_dir / ".git").mkdir()
    (source_dir / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    (source_dir / ".build").mkdir()
    (source_dir / ".build" / "main.pdf").write_bytes(b"%PDF")

    copy = service.duplicate_project(meta.project_id)
    assert copy.project_id == "original-copy"
    assert copy.name == "Original (copy)"
    assert copy.root_tex == meta.root_tex

    copy_dir = service.get_project_dir(copy.project_id)
    assert (copy_dir / "main.tex").read_text(encoding="utf-8") == (source_dir / "main.tex").read_text(encoding="utf-8")
    assert (copy_dir / "chapters" / "ch1.tex").read_text(encoding="utf-8") == "Chapter one"
    assert not (copy_dir / ".git").exists()
    assert not (copy_dir / ".build").exists()

    second = service.duplicate_project(meta.project_id)
    assert second.project_id == "original-copy-2"
    named = service.duplicate_project(meta.project_id, name="Named Copy")
    assert named.name == "Named Copy"
    assert {p.project_id for p in service.list_projects()} == {
        "original",
        "original-copy",
        "original-copy-2",
        "named-copy",
    }


def test_delete_removes_folder_and_project_rows(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    keep = service.create_project(name="Keep")
    doomed = service.create_project(name="Doomed")
    doomed_dir = service.get_project_dir(doomed.project_id)

    with closing(service.db.get_connection()) as conn:
        for pid in (keep.project_id, doomed.project_id):
            conn.execute(
                "INSERT INTO chats (chat_id, project_id, title, created_at, updated_at) VALUES (?, ?, 't', 'x', 'x');",
                (f"chat_{pid}", pid),
            )
            conn.execute(
                "INSERT INTO chat_messages (message_id, chat_id, project_id, role, content, created_at) "
                "VALUES (?, ?, ?, 'user', 'hi', 'x');",
                (f"msg_{pid}", f"chat_{pid}", pid),
            )

    service.delete_project(doomed.project_id)

    assert not doomed_dir.exists()
    assert [p.project_id for p in service.list_projects()] == [keep.project_id]
    with closing(service.db.get_connection()) as conn:
        assert conn.execute("SELECT project_id FROM chats;").fetchall()[0]["project_id"] == keep.project_id
        assert conn.execute("SELECT COUNT(*) AS n FROM chat_messages;").fetchone()["n"] == 1
    with pytest.raises(KeyError):
        service.delete_project(doomed.project_id)


def test_folders_move_and_delete(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    meta = service.create_project(name="Organised", template="blank")
    pid = meta.project_id

    assert service.create_folder(pid, "chapters") == "chapters"
    assert "chapters" in service.list_folders(pid)
    with pytest.raises(FileExistsError):
        service.create_folder(pid, "chapters")
    with pytest.raises(PermissionError):
        service.create_folder(pid, ".hidden")

    service.write_file(pid, "intro.tex", "Intro")
    new_path, _ = service.move_path(pid, "intro.tex", "chapters/intro.tex")
    assert new_path == "chapters/intro.tex"
    assert service.read_file(pid, "chapters/intro.tex").text == "Intro"
    with pytest.raises(FileExistsError):
        service.move_path(pid, "main.tex", "chapters/intro.tex")
    with pytest.raises(PermissionError):
        service.move_path(pid, "chapters", "chapters/nested")

    # Moving the root .tex file (or its folder) keeps root_tex pointing at it.
    _, moved = service.move_path(pid, "main.tex", "chapters/main.tex")
    assert moved.root_tex == "chapters/main.tex"
    _, moved = service.move_path(pid, "chapters", "parts")
    assert moved.root_tex == "parts/main.tex"
    assert service.list_folders(pid) == ["parts"]

    service.delete_file(pid, "parts/intro.tex")
    assert not (service.get_project_dir(pid) / "parts" / "intro.tex").exists()
    service.delete_folder(pid, "parts")
    assert "parts" not in service.list_folders(pid)
    with pytest.raises(FileNotFoundError):
        service.delete_folder(pid, "parts")
