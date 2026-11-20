from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db import DatabaseManager


def test_fresh_db_initializes_v1_schema_and_wal_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    backup_dir = tmp_path / "backups"

    manager = DatabaseManager(db_path=db_path, backup_dir=backup_dir)
    version = manager.initialize()

    assert version == 7
    assert db_path.is_file()

    with manager.get_connection() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal"

        fk_enabled = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        assert fk_enabled == 1

        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        ).fetchall()
        table_names = {row[0] for row in rows}

        expected_tables = {
            "schema_migrations",
            "projects",
            "sessions",
            "chats",
            "chat_messages",
            "candidate_sources",
            "pdf_documents",
            "pdf_chunks",
            "scholarly_cache",
            "spend_ledger",
            "github_links",
            "project_index_files",
        }
        assert expected_tables.issubset(table_names)

        applied = conn.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version;"
        ).fetchall()
        assert [a[0] for a in applied] == [1, 2, 3, 4, 5, 6, 7]
        assert all(a[1] for a in applied)


def test_v2_creates_enabled_models_tables(tmp_path: Path) -> None:
    manager = DatabaseManager(
        db_path=tmp_path / "app.db", backup_dir=tmp_path / "backups"
    )
    assert manager.initialize() == 7

    with manager.get_connection() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        assert {"enabled_models", "model_registry_state"}.issubset(tables)
        state = conn.execute(
            "SELECT id, seeded FROM model_registry_state;"
        ).fetchall()
        assert [(r[0], r[1]) for r in state] == [(1, 0)]


def test_v2_upgrades_existing_v1_database(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    backup_dir = tmp_path / "backups"

    v1_only = DatabaseManager(db_path=db_path, backup_dir=backup_dir)
    v1_only._migrations = {1: v1_only._migrations[1]}
    assert v1_only.initialize() == 1
    with v1_only.get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (project_id, name, created_at, updated_at) "
            "VALUES ('p1', 'Keep me', 'now', 'now');"
        )

    upgraded = DatabaseManager(db_path=db_path, backup_dir=backup_dir)
    assert upgraded.initialize() == 7

    with upgraded.get_connection() as conn:
        row = conn.execute(
            "SELECT name, tex_engine FROM projects WHERE project_id = 'p1';"
        ).fetchone()
        assert row is not None and row[0] == "Keep me" and row[1] == "pdflatex"
        assert conn.execute("SELECT COUNT(*) FROM enabled_models;").fetchone()[0] == 0
        state = conn.execute("SELECT id, seeded FROM model_registry_state;").fetchall()
        assert [tuple(r) for r in state] == [(1, 0)]
    assert len(list(backup_dir.glob("app.db.v1.*.bak"))) == 1


def test_pending_migration_creates_backup_before_applying(tmp_path: Path) -> None:
    db_path = tmp_path / "app.db"
    backup_dir = tmp_path / "backups"

    manager = DatabaseManager(db_path=db_path, backup_dir=backup_dir)
    # Isolate the backup mechanism from the real migrations beyond v1.
    manager._migrations = {1: manager._migrations[1]}
    assert manager.initialize() == 1
    assert list(backup_dir.glob("*.bak")) == []

    # Register a valid v2 migration and apply it on existing database
    manager.register_migration(
        2,
        "CREATE TABLE synthetic_v2 (id INTEGER PRIMARY KEY, label TEXT NOT NULL);",
    )
    new_version = manager.run_migrations(target_version=2)
    assert new_version == 2

    backups = list(backup_dir.glob("app.db.v1.*.bak"))
    assert len(backups) == 1
    assert backups[0].stat().st_size > 0

    # Register a failing v3 migration and verify rollback + backup creation
    manager.register_migration(
        3,
        "CREATE TABLE partial_v3 (id INTEGER PRIMARY KEY); INVALID SQL SYNTAX HERE;",
    )
    with pytest.raises(sqlite3.Error):
        manager.run_migrations(target_version=3)

    v2_backups = list(backup_dir.glob("app.db.v2.*.bak"))
    assert len(v2_backups) == 1

    with manager.get_connection() as conn:
        current_version = conn.execute(
            "SELECT MAX(version) FROM schema_migrations;"
        ).fetchone()[0]
        assert current_version == 2
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        assert "synthetic_v2" in tables
        assert "partial_v3" not in tables


def test_v5_migration_creates_project_index_files_table(tmp_path: Path) -> None:
    manager = DatabaseManager(
        db_path=tmp_path / "app.db", backup_dir=tmp_path / "backups"
    )
    version = manager.initialize()
    assert version == 7

    with manager.get_connection() as conn:
        cols = conn.execute("PRAGMA table_info(project_index_files);").fetchall()
        col_names = [row["name"] for row in cols]
        assert col_names == [
            "project_id",
            "rel_path",
            "content_hash",
            "mtime_ns",
            "line_count",
            "index_json",
            "updated_at",
        ]

        pk_cols = [row["name"] for row in cols if row["pk"] > 0]
        assert pk_cols == ["project_id", "rel_path"]

        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='project_index_files';"
            ).fetchall()
        }
        assert "idx_project_index_files_project" in indexes

        versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert 5 in versions


def test_v5_migration_cascades_on_project_delete(tmp_path: Path) -> None:
    manager = DatabaseManager(
        db_path=tmp_path / "app.db", backup_dir=tmp_path / "backups"
    )
    assert manager.initialize() == 7

    with manager.get_connection() as conn:
        conn.execute(
            "INSERT INTO projects (project_id, name, created_at, updated_at) "
            "VALUES ('p1', 'Test Project', 'now', 'now');"
        )
        conn.execute(
            "INSERT INTO project_index_files "
            "(project_id, rel_path, content_hash, mtime_ns, line_count, index_json, updated_at) "
            "VALUES ('p1', 'main.tex', 'deadbeef', 123456789, 42, '{}', 'now');"
        )
        count_before = conn.execute(
            "SELECT COUNT(*) FROM project_index_files WHERE project_id = 'p1';"
        ).fetchone()[0]
        assert count_before == 1

        conn.execute("DELETE FROM projects WHERE project_id = 'p1';")
        count_after = conn.execute(
            "SELECT COUNT(*) FROM project_index_files WHERE project_id = 'p1';"
        ).fetchone()[0]
        assert count_after == 0
