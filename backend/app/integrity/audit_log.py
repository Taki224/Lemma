from __future__ import annotations

import json
import os
import secrets
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.projects.service import ProjectService

AUDIT_LOG_FILENAME = ".ai-audit-log.json"
AUDIT_LOG_SCHEMA_VERSION = 1
DEFAULT_GUIDELINES_PROFILE = "eu_living_guidelines_2026"


def _safe_int(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


@dataclass
class AuditLogEntry:
    entry_id: str
    timestamp: str
    source_surface: str  # "chat" | "quick_action" | "review_studio" | "manual"
    provider: str  # "openrouter" | "local" | "manual"
    model_id: str
    model_display_name: str
    tier: int  # 1 | 2 | 3
    category: str
    purpose: str
    affected_files: list[str] = field(default_factory=list)
    section_hint: str = ""
    prompt_summary: str = ""
    action_summary: str = ""
    outcome: str = "accepted"  # "accepted" | "logged_from_chat" | "manual"
    human_verified: bool = False
    verification_note: str = ""
    chat_id: str | None = None
    message_id: str | None = None
    hunk_id: str | None = None
    flags: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AuditLogEntry:
        return cls(
            entry_id=str(raw.get("entry_id") or f"ailog_{secrets.token_hex(6)}"),
            timestamp=str(raw.get("timestamp") or datetime.now(timezone.utc).isoformat()),
            source_surface=str(raw.get("source_surface") or "chat"),
            provider=str(raw.get("provider") or "openrouter"),
            model_id=str(raw.get("model_id") or ""),
            model_display_name=str(raw.get("model_display_name") or raw.get("model_id") or ""),
            tier=max(1, min(3, _safe_int(raw.get("tier"), 2))),
            category=str(raw.get("category") or "substantive_drafting"),
            purpose=str(raw.get("purpose") or ""),
            affected_files=[str(f) for f in (raw.get("affected_files") or [])],
            section_hint=str(raw.get("section_hint") or ""),
            prompt_summary=str(raw.get("prompt_summary") or ""),
            action_summary=str(raw.get("action_summary") or ""),
            outcome=str(raw.get("outcome") or "accepted"),
            human_verified=bool(raw.get("human_verified", False)),
            verification_note=str(raw.get("verification_note") or ""),
            chat_id=raw.get("chat_id"),
            message_id=raw.get("message_id"),
            hunk_id=raw.get("hunk_id"),
            flags={str(k): bool(v) for k, v in (raw.get("flags") or {}).items()},
        )


@dataclass
class AuditLogFile:
    schema_version: int
    project_id: str
    guidelines_profile: str
    updated_at: str
    entries: list[AuditLogEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "guidelines_profile": self.guidelines_profile,
            "updated_at": self.updated_at,
            "entries": [e.to_dict() for e in self.entries],
        }


class AuditLogStore:
    """Atomic JSON persistence for <project_dir>/.ai-audit-log.json."""

    def __init__(self, project_service: ProjectService) -> None:
        self._project_service = project_service

    def _log_path(self, project_id: str) -> Path:
        self._project_service.get_project(project_id)
        project_dir = self._project_service.get_project_dir(project_id)
        return project_dir / AUDIT_LOG_FILENAME

    def _read_unlocked(self, project_id: str, path: Path) -> AuditLogFile:
        now_iso = datetime.now(timezone.utc).isoformat()
        if not path.is_file():
            return AuditLogFile(
                schema_version=AUDIT_LOG_SCHEMA_VERSION,
                project_id=project_id,
                guidelines_profile=DEFAULT_GUIDELINES_PROFILE,
                updated_at=now_iso,
                entries=[],
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AuditLogFile(
                schema_version=AUDIT_LOG_SCHEMA_VERSION,
                project_id=project_id,
                guidelines_profile=DEFAULT_GUIDELINES_PROFILE,
                updated_at=now_iso,
                entries=[],
            )
        if not isinstance(raw, dict):
            return AuditLogFile(
                schema_version=AUDIT_LOG_SCHEMA_VERSION,
                project_id=project_id,
                guidelines_profile=DEFAULT_GUIDELINES_PROFILE,
                updated_at=now_iso,
                entries=[],
            )
        raw_entries = raw.get("entries")
        entries = [
            AuditLogEntry.from_dict(item)
            for item in (raw_entries if isinstance(raw_entries, list) else [])
            if isinstance(item, dict)
        ]
        return AuditLogFile(
            schema_version=_safe_int(raw.get("schema_version"), AUDIT_LOG_SCHEMA_VERSION),
            project_id=project_id,
            guidelines_profile=str(raw.get("guidelines_profile") or DEFAULT_GUIDELINES_PROFILE),
            updated_at=str(raw.get("updated_at") or now_iso),
            entries=entries,
        )

    def _write_unlocked(self, path: Path, log_file: AuditLogFile) -> None:
        log_file.updated_at = datetime.now(timezone.utc).isoformat()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(log_file.to_dict(), indent=2, ensure_ascii=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(
            prefix=".ai-audit-log.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(payload)
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def read_log(self, project_id: str) -> AuditLogFile:
        path = self._log_path(project_id)
        with self._project_service.file_lock(project_id):
            return self._read_unlocked(project_id, path)

    def append_entry(
        self,
        project_id: str,
        *,
        source_surface: str,
        provider: str,
        model_id: str,
        model_display_name: str = "",
        tier: int = 2,
        category: str = "substantive_drafting",
        purpose: str = "",
        affected_files: list[str] | None = None,
        section_hint: str = "",
        prompt_summary: str = "",
        action_summary: str = "",
        outcome: str = "accepted",
        human_verified: bool = False,
        verification_note: str = "",
        chat_id: str | None = None,
        message_id: str | None = None,
        hunk_id: str | None = None,
        flags: dict[str, bool] | None = None,
    ) -> AuditLogEntry:
        path = self._log_path(project_id)
        with self._project_service.file_lock(project_id):
            log_file = self._read_unlocked(project_id, path)

            # Deduplicate by hunk_id if already logged
            if hunk_id:
                for existing in log_file.entries:
                    if existing.hunk_id == hunk_id:
                        return existing

            entry = AuditLogEntry(
                entry_id=f"ailog_{secrets.token_hex(6)}",
                timestamp=datetime.now(timezone.utc).isoformat(),
                source_surface=source_surface,
                provider=provider or "openrouter",
                model_id=model_id,
                model_display_name=model_display_name or model_id,
                tier=max(1, min(3, int(tier))),
                category=category,
                purpose=purpose.strip(),
                affected_files=[str(f) for f in (affected_files or []) if str(f).strip()],
                section_hint=section_hint.strip(),
                prompt_summary=prompt_summary.strip()[:500],
                action_summary=action_summary.strip()[:500],
                outcome=outcome,
                human_verified=bool(human_verified),
                verification_note=verification_note.strip(),
                chat_id=chat_id,
                message_id=message_id,
                hunk_id=hunk_id,
                flags={str(k): bool(v) for k, v in (flags or {}).items()},
            )
            log_file.entries.append(entry)
            self._write_unlocked(path, log_file)
            return entry

    def update_entry(
        self,
        project_id: str,
        entry_id: str,
        *,
        purpose: str | None = None,
        affected_files: list[str] | None = None,
        section_hint: str | None = None,
        prompt_summary: str | None = None,
        action_summary: str | None = None,
        human_verified: bool | None = None,
        verification_note: str | None = None,
    ) -> AuditLogEntry:
        path = self._log_path(project_id)
        with self._project_service.file_lock(project_id):
            log_file = self._read_unlocked(project_id, path)
            target: AuditLogEntry | None = None
            for entry in log_file.entries:
                if entry.entry_id == entry_id:
                    target = entry
                    break
            if target is None:
                raise KeyError(f"Audit log entry not found: {entry_id}")

            if purpose is not None:
                target.purpose = purpose.strip()
            if affected_files is not None:
                target.affected_files = [str(f) for f in affected_files if str(f).strip()]
            if section_hint is not None:
                target.section_hint = section_hint.strip()
            if prompt_summary is not None:
                target.prompt_summary = prompt_summary.strip()[:500]
            if action_summary is not None:
                target.action_summary = action_summary.strip()[:500]
            if human_verified is not None:
                target.human_verified = bool(human_verified)
            if verification_note is not None:
                target.verification_note = verification_note.strip()

            self._write_unlocked(path, log_file)
            return target

    def delete_entry(self, project_id: str, entry_id: str) -> None:
        path = self._log_path(project_id)
        with self._project_service.file_lock(project_id):
            log_file = self._read_unlocked(project_id, path)
            before = len(log_file.entries)
            log_file.entries = [e for e in log_file.entries if e.entry_id != entry_id]
            if len(log_file.entries) == before:
                raise KeyError(f"Audit log entry not found: {entry_id}")
            self._write_unlocked(path, log_file)
