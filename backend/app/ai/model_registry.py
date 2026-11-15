from __future__ import annotations

import sqlite3
import time
from contextlib import closing, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.db import DatabaseManager

if TYPE_CHECKING:
    from app.ai.openrouter import OpenRouterModel


@dataclass(frozen=True)
class EnabledModel:
    id: str
    name: str
    context_length: int
    prompt_price_per_token: float
    completion_price_per_token: float
    supports_vision: bool
    verified: bool
    provider: str = "openrouter"
    base_url: str = ""


def _row_to_model(row: sqlite3.Row) -> EnabledModel:
    keys = row.keys()
    return EnabledModel(
        id=str(row["model_id"]),
        name=str(row["name"]),
        context_length=int(row["context_length"]),
        prompt_price_per_token=float(row["prompt_price_per_token"]),
        completion_price_per_token=float(row["completion_price_per_token"]),
        supports_vision=bool(row["supports_vision"]),
        verified=bool(row["verified"]),
        provider=str(row["provider"]) if "provider" in keys and row["provider"] else "openrouter",
        base_url=str(row["base_url"]) if "base_url" in keys and row["base_url"] else "",
    )


class ModelRegistry:
    """SQLite-backed list of OpenRouter models the user has enabled."""

    def __init__(self, db: DatabaseManager) -> None:
        self.db = db

    def list_enabled(self) -> list[EnabledModel]:
        with closing(self.db.get_connection()) as conn:
            rows = conn.execute(
                "SELECT * FROM enabled_models ORDER BY position ASC;"
            ).fetchall()
        return [_row_to_model(r) for r in rows]

    def is_enabled(self, model_id: str) -> bool:
        with closing(self.db.get_connection()) as conn:
            row = conn.execute(
                "SELECT 1 FROM enabled_models WHERE model_id = ?;", (model_id,)
            ).fetchone()
        return row is not None

    def get_model(self, model_id: str) -> EnabledModel | None:
        with closing(self.db.get_connection()) as conn:
            row = conn.execute(
                "SELECT * FROM enabled_models WHERE model_id = ?;", (model_id,)
            ).fetchone()
        return _row_to_model(row) if row is not None else None

    def enable(self, model: EnabledModel) -> EnabledModel:
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                self._upsert(conn, model)
                conn.execute("COMMIT;")
            except Exception:
                with suppress(sqlite3.Error):
                    conn.execute("ROLLBACK;")
                raise
        return model

    def seed_if_needed(self, seed: list[EnabledModel]) -> bool:
        with closing(self.db.get_connection()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                row = conn.execute(
                    "SELECT seeded FROM model_registry_state WHERE id = 1;"
                ).fetchone()
                if row is not None and int(row["seeded"]) != 0:
                    conn.execute("ROLLBACK;")
                    return False
                for model in seed:
                    self._upsert(conn, model)
                conn.execute(
                    "INSERT INTO model_registry_state (id, seeded) VALUES (1, 1) "
                    "ON CONFLICT(id) DO UPDATE SET seeded = 1;"
                )
                conn.execute("COMMIT;")
                return True
            except Exception:
                with suppress(sqlite3.Error):
                    conn.execute("ROLLBACK;")
                raise

    def disable(self, model_id: str) -> bool:
        with closing(self.db.get_connection()) as conn:
            cursor = conn.execute(
                "DELETE FROM enabled_models WHERE model_id = ?;", (model_id,)
            )
            return cursor.rowcount > 0

    @staticmethod
    def _upsert(conn: sqlite3.Connection, model: EnabledModel) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM enabled_models;"
        ).fetchone()
        conn.execute(
            """
            INSERT INTO enabled_models (
                model_id, name, context_length, prompt_price_per_token,
                completion_price_per_token, supports_vision, verified,
                provider, base_url, position, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_id) DO UPDATE SET
                name = excluded.name,
                context_length = excluded.context_length,
                prompt_price_per_token = excluded.prompt_price_per_token,
                completion_price_per_token = excluded.completion_price_per_token,
                supports_vision = excluded.supports_vision,
                verified = excluded.verified,
                provider = excluded.provider,
                base_url = excluded.base_url;
            """,
            (
                model.id,
                model.name,
                model.context_length,
                model.prompt_price_per_token,
                model.completion_price_per_token,
                int(model.supports_vision),
                int(model.verified),
                model.provider,
                model.base_url,
                int(row["next_position"]),
                int(time.time()),
            ),
        )


def build_seed_models(
    allowlist: list[str], fallback: list[OpenRouterModel]
) -> list[EnabledModel]:
    """Build the first-run model list from MODEL_ALLOWLIST, or all fallback models if unset."""
    known = {m.id: m for m in fallback}
    if not allowlist:
        return [_from_catalog(m) for m in fallback]
    seeded: list[EnabledModel] = []
    for model_id in allowlist:
        match = known.get(model_id)
        if match is not None:
            seeded.append(_from_catalog(match))
        else:
            seeded.append(
                EnabledModel(
                    id=model_id,
                    name=model_id,
                    context_length=128000,
                    prompt_price_per_token=0.000003,
                    completion_price_per_token=0.000015,
                    supports_vision=False,
                    verified=False,
                )
            )
    return seeded


def _from_catalog(model: OpenRouterModel) -> EnabledModel:
    return EnabledModel(
        id=model.id,
        name=model.name,
        context_length=model.context_length,
        prompt_price_per_token=model.prompt_price_per_token,
        completion_price_per_token=model.completion_price_per_token,
        supports_vision=model.supports_vision,
        verified=True,
    )
