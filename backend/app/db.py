from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

V1_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    root_tex TEXT,
    zotero_collection_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    csrf_token TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    last_active INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chats (
    chat_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_messages (
    message_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    model TEXT,
    profile_id TEXT,
    hunks_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS candidate_sources (
    candidate_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    cite_key TEXT NOT NULL,
    title TEXT NOT NULL,
    authors_json TEXT NOT NULL,
    year INTEGER,
    doi TEXT,
    abstract TEXT,
    source_api TEXT NOT NULL,
    bibtex TEXT NOT NULL,
    grounding_tier TEXT NOT NULL,
    pdf_path TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'staged'
);

CREATE INDEX IF NOT EXISTS idx_candidate_sources_project_status
    ON candidate_sources(project_id, status);

CREATE TABLE IF NOT EXISTS pdf_documents (
    doc_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    cite_key TEXT,
    pdf_path TEXT NOT NULL,
    page_count INTEGER NOT NULL DEFAULT 0,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pdf_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    cite_key TEXT,
    page_number INTEGER NOT NULL,
    text TEXT NOT NULL,
    injection_flagged INTEGER NOT NULL DEFAULT 0,
    injection_matches_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(doc_id) REFERENCES pdf_documents(doc_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pdf_chunks_project_cite
    ON pdf_chunks(project_id, cite_key);

CREATE TABLE IF NOT EXISTS scholarly_cache (
    cache_key TEXT PRIMARY KEY,
    source_api TEXT NOT NULL,
    query_or_doi TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS spend_ledger (
    reservation_id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    model TEXT NOT NULL,
    reserved_usd REAL NOT NULL,
    actual_usd REAL,
    status TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_spend_ledger_created_status
    ON spend_ledger(created_at, status);
"""


V2_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS enabled_models (
    model_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    context_length INTEGER NOT NULL,
    prompt_price_per_token REAL NOT NULL,
    completion_price_per_token REAL NOT NULL,
    supports_vision INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 1,
    position INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS model_registry_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    seeded INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO model_registry_state (id, seeded) VALUES (1, 0);
"""


V3_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS github_links (
    project_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    subpath TEXT NOT NULL DEFAULT '',
    last_synced_sha TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
"""

V4_MIGRATION_SQL = """
ALTER TABLE chat_messages ADD COLUMN references_json TEXT;
"""

V5_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS project_index_files (
    project_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    line_count INTEGER NOT NULL,
    index_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, rel_path),
    FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_project_index_files_project
    ON project_index_files(project_id);
"""

V6_MIGRATION_SQL = """
ALTER TABLE enabled_models ADD COLUMN provider TEXT NOT NULL DEFAULT 'openrouter';
ALTER TABLE enabled_models ADD COLUMN base_url TEXT NOT NULL DEFAULT '';
"""

V7_MIGRATION_SQL = """
ALTER TABLE projects ADD COLUMN tex_engine TEXT NOT NULL DEFAULT 'pdflatex';
"""


def _split_sql_statements(sql_script: str) -> list[str]:
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql_script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buffer.append(line)
        candidate = "\n".join(buffer)
        if sqlite3.complete_statement(candidate):
            statements.append(candidate.strip())
            buffer.clear()
    if buffer:
        remainder = "\n".join(buffer).strip()
        if remainder:
            for part in remainder.split(";"):
                if part.strip():
                    statements.append(part.strip() + ";")
    return statements


class DatabaseManager:
    """SQLite connection and migration manager with automatic pre-migration backups."""

    def __init__(self, db_path: Path, backup_dir: Path) -> None:
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir)
        self._migrations: dict[int, str] = {
            1: V1_MIGRATION_SQL,
            2: V2_MIGRATION_SQL,
            3: V3_MIGRATION_SQL,
            4: V4_MIGRATION_SQL,
            5: V5_MIGRATION_SQL,
            6: V6_MIGRATION_SQL,
            7: V7_MIGRATION_SQL,
        }

    def register_migration(self, version: int, sql_script: str) -> None:
        if version < 1:
            raise ValueError("Migration version must be >= 1")
        self._migrations[version] = sql_script

    def get_connection(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        if self.db_path.is_file():
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                pass
        return conn

    def _ensure_migrations_table(self, conn: sqlite3.Connection) -> int:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """
        )
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS current_version FROM schema_migrations;"
        ).fetchone()
        return int(row["current_version"]) if row is not None else 0

    def _create_backup(self, current_version: int) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = (
            self.backup_dir / f"{self.db_path.name}.v{current_version}.{timestamp}.bak"
        )
        src_conn = self.get_connection()
        try:
            dest_conn = sqlite3.connect(str(backup_path))
            try:
                src_conn.backup(dest_conn)
            finally:
                dest_conn.close()
        finally:
            src_conn.close()
        try:
            os.chmod(backup_path, 0o600)
        except OSError:
            pass
        return backup_path

    def initialize(self) -> int:
        """Initialize the SQLite database file and apply all registered migrations."""
        return self.run_migrations()

    def run_migrations(self, target_version: int | None = None) -> int:
        """Apply pending schema migrations up to target_version, backing up existing DBs first."""
        existed_before = self.db_path.is_file() and self.db_path.stat().st_size > 0

        with self.get_connection() as conn:
            current_version = self._ensure_migrations_table(conn)

        max_target = (
            target_version
            if target_version is not None
            else max(self._migrations.keys(), default=0)
        )
        pending_versions = sorted(
            v for v in self._migrations if current_version < v <= max_target
        )
        if not pending_versions:
            return current_version

        if existed_before and current_version > 0:
            self._create_backup(current_version)

        with self.get_connection() as conn:
            for version in pending_versions:
                sql_script = self._migrations[version]
                statements = _split_sql_statements(sql_script)
                applied_at = datetime.now(timezone.utc).isoformat()
                conn.execute("BEGIN IMMEDIATE;")
                try:
                    for stmt in statements:
                        conn.execute(stmt)
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                        (version, applied_at),
                    )
                    conn.execute("COMMIT;")
                    current_version = version
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

        return current_version
