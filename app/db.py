from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DB_PATH


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                language TEXT NOT NULL DEFAULT 'ko',
                style_bible TEXT NOT NULL DEFAULT '',
                default_voice TEXT NOT NULL DEFAULT 'local-command',
                subtitle_settings TEXT NOT NULL DEFAULT '{}',
                bgm_path TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                description_json TEXT NOT NULL,
                reference_image_paths TEXT NOT NULL DEFAULT '[]',
                locked_reference_path TEXT,
                identity_lock TEXT NOT NULL DEFAULT '',
                voice_gender TEXT NOT NULL DEFAULT '',
                voice_name TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                number INTEGER NOT NULL DEFAULT 1,
                title TEXT NOT NULL,
                manuscript TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'DRAFT',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scenes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                scene_order INTEGER NOT NULL,
                source_start INTEGER NOT NULL,
                source_end INTEGER NOT NULL,
                summary TEXT NOT NULL,
                characters_present TEXT NOT NULL DEFAULT '[]',
                location_time TEXT NOT NULL DEFAULT '',
                visual_action TEXT NOT NULL DEFAULT '',
                mood TEXT NOT NULL DEFAULT '',
                prompt TEXT NOT NULL DEFAULT '',
                negative_constraints TEXT NOT NULL DEFAULT '',
                image_path TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                generation_metadata TEXT NOT NULL DEFAULT '{}',
                UNIQUE(episode_id, scene_order)
            );

            CREATE TABLE IF NOT EXISTS narration_segments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                segment_order INTEGER NOT NULL,
                text TEXT NOT NULL,
                audio_path TEXT,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS renders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                status TEXT NOT NULL,
                output_path TEXT,
                settings_json TEXT NOT NULL DEFAULT '{}',
                error_log TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                label TEXT NOT NULL,
                status TEXT NOT NULL,
                progress_current INTEGER NOT NULL DEFAULT 0,
                progress_total INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                error TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT
            );

            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                epub_path TEXT NOT NULL,
                tts_provider TEXT NOT NULL DEFAULT 'google',
                tts_voice TEXT NOT NULL DEFAULT '',
                speed REAL NOT NULL DEFAULT 1.0,
                selected_chapters TEXT NOT NULL DEFAULT '[]',
                progress_chapter_id INTEGER,
                progress_chunk_order INTEGER NOT NULL DEFAULT 0,
                progress_offset_sec REAL NOT NULL DEFAULT 0,
                progress_updated_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS book_chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                chapter_order INTEGER NOT NULL,
                title TEXT NOT NULL,
                char_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(book_id, chapter_order)
            );

            CREATE TABLE IF NOT EXISTS book_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                chapter_id INTEGER NOT NULL REFERENCES book_chapters(id) ON DELETE CASCADE,
                chunk_order INTEGER NOT NULL,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                UNIQUE(chapter_id, chunk_order)
            );

            CREATE TABLE IF NOT EXISTS book_tts_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                voice TEXT NOT NULL,
                chars INTEGER NOT NULL,
                cache_hit INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS provider_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                action TEXT NOT NULL,
                scene_order INTEGER,
                call_count INTEGER NOT NULL DEFAULT 0,
                asset_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            """
        )
        _ensure_column(conn, "projects", "use_ai_images", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "projects", "use_subtitles", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "projects", "use_bgm", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "characters", "locked_reference_path", "TEXT")
        _ensure_column(conn, "characters", "identity_lock", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "characters", "voice_gender", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "characters", "voice_name", "TEXT NOT NULL DEFAULT ''")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def rows(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    with connect() as conn:
        cur = conn.execute(sql, params)
        return int(cur.lastrowid)


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def from_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def relative(path: Path | None) -> str | None:
    if path is None:
        return None
    return str(path)
