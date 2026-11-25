"""SQLite 存储层（WAL + 短连接 + FTS5 trigram 可选）。

所有公开方法都是 async，内部通过 asyncio.to_thread 执行同步 sqlite 调用，
避免阻塞 AstrBot 的事件循环。写操作串行化。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import MemeCandidate, Segment, SyncResult, UpsertResult, VideoInfo
from .text import clean_text, is_meaningful_term, matches_in_text, normalize_term

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL DEFAULT 'bilibili',
    video_id TEXT NOT NULL,
    bvid TEXT NOT NULL DEFAULT '',
    aid INTEGER NOT NULL DEFAULT 0,
    cid INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    owner_name TEXT NOT NULL DEFAULT '',
    owner_mid INTEGER NOT NULL DEFAULT 0,
    pubdate INTEGER NOT NULL DEFAULT 0,
    duration INTEGER NOT NULL DEFAULT 0,
    season_id INTEGER NOT NULL DEFAULT 0,
    page_no INTEGER NOT NULL DEFAULT 1,
    is_charge_blocked INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT NOT NULL DEFAULT '',
    source_label TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (platform, video_id)
);

CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    lang TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    segments_json TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    char_count INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL,
    raw_deleted_at TEXT,
    UNIQUE (video_id, lang, provider, text_hash)
);

CREATE TABLE IF NOT EXISTS memes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL,
    norm_term TEXT NOT NULL UNIQUE,
    aliases TEXT NOT NULL DEFAULT '[]',
    definition TEXT NOT NULL DEFAULT '',
    usage TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT '',
    examples TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.5,
    heat INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    memewiki_slug TEXT NOT NULL DEFAULT '',
    sync_state TEXT NOT NULL DEFAULT 'pending',
    sync_error TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL DEFAULT '',
    last_seen TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meme_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meme_id INTEGER NOT NULL REFERENCES memes(id) ON DELETE CASCADE,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    start_ms INTEGER NOT NULL DEFAULT 0,
    quote TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (meme_id, video_id, start_ms)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    dedupe_key TEXT UNIQUE,
    payload TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS group_slangs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    term TEXT NOT NULL,
    norm_term TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    definition TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (scope, norm_term)
);

CREATE TABLE IF NOT EXISTS auth_accounts (
    mid INTEGER PRIMARY KEY,
    uname TEXT NOT NULL DEFAULT '',
    cookies_enc TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'qrcode',
    status TEXT NOT NULL DEFAULT 'ok',
    last_ok_at TEXT NOT NULL DEFAULT '',
    expires_hint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL DEFAULT 'info',
    action TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_season ON videos(season_id);
CREATE INDEX IF NOT EXISTS idx_transcripts_video ON transcripts(video_id);
CREATE INDEX IF NOT EXISTS idx_memes_status ON memes(status);
CREATE INDEX IF NOT EXISTS idx_memes_sync ON memes(sync_state);
CREATE INDEX IF NOT EXISTS idx_sources_meme ON meme_sources(meme_id);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue(status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_slangs_scope ON group_slangs(scope);
"""

FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS memes_fts USING fts5(
    term, aliases, definition,
    content='memes', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS memes_fts_ai AFTER INSERT ON memes BEGIN
    INSERT INTO memes_fts(rowid, term, aliases, definition)
    VALUES (new.id, new.term, new.aliases, new.definition);
END;
CREATE TRIGGER IF NOT EXISTS memes_fts_ad AFTER DELETE ON memes BEGIN
    INSERT INTO memes_fts(memes_fts, rowid, term, aliases, definition)
    VALUES ('delete', old.id, old.term, old.aliases, old.definition);
END;
CREATE TRIGGER IF NOT EXISTS memes_fts_au AFTER UPDATE ON memes BEGIN
    INSERT INTO memes_fts(memes_fts, rowid, term, aliases, definition)
    VALUES ('delete', old.id, old.term, old.aliases, old.definition);
    INSERT INTO memes_fts(rowid, term, aliases, definition)
    VALUES (new.id, new.term, new.aliases, new.definition);
END;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [clean_text(str(v)) for v in value if clean_text(str(v))]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [clean_text(str(v)) for v in parsed if clean_text(str(v))]
        except (ValueError, TypeError):
            return [clean_text(value)]
    return []


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _merge_ci(existing: list[str], incoming: list[str]) -> list[str]:
    """大小写不敏感合并别名，保留首次出现的写法。"""
    seen = {item.lower() for item in existing if item}
    merged = list(existing)
    for item in incoming:
        key = item.lower()
        if key and key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def _decode_task(row: dict[str, Any]) -> dict[str, Any]:
    """tasks.payload 落库是 JSON 字符串，读出来还原成对象给前端用。"""
    payload = row.get("payload")
    if isinstance(payload, str) and payload:
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            parsed = {}
        row["payload"] = parsed if isinstance(parsed, dict) else {}
    elif not isinstance(payload, dict):
        row["payload"] = {}
    return row


class Storage:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._write_lock: asyncio.Lock = asyncio.Lock()
        self._fts = False
        self._opened = False

    # ------------------------------------------------------------------ 基础

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=20, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _migrate_sync(conn: sqlite3.Connection) -> None:
        """轻量迁移：只加列，不改旧数据。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
        if "source_label" not in columns:
            conn.execute("ALTER TABLE videos ADD COLUMN source_label TEXT NOT NULL DEFAULT ''")
        if "url" not in columns:
            conn.execute("ALTER TABLE videos ADD COLUMN url TEXT NOT NULL DEFAULT ''")

        # 归一化键统一小写：旧库里混排词名（K神/k神）可能带着原始大小写
        # 逐行 UPDATE，撞 UNIQUE 的行单独跳过，不让整条迁移静默回滚
        for table in ("memes", "group_slangs"):
            rows = conn.execute(
                f"SELECT rowid, norm_term FROM {table} WHERE norm_term <> lower(norm_term)"
            ).fetchall()
            for rowid, value in rows:
                lowered = str(value).lower()
                exists = conn.execute(
                    f"SELECT 1 FROM {table} WHERE norm_term=? AND rowid<>?",
                    (lowered, rowid),
                ).fetchone()
                if exists is None:
                    try:
                        conn.execute(
                            f"UPDATE {table} SET norm_term=? WHERE rowid=?", (lowered, rowid)
                        )
                    except sqlite3.IntegrityError:
                        pass

    def _init_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA_SQL)
            self._migrate_sync(conn)
            try:
                conn.executescript(FTS_SQL)
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES('schema_version', ?, ?)",
                (str(1), utcnow()),
            )

    async def open(self) -> "Storage":
        if not self._opened:
            await asyncio.to_thread(self._init_sync)
            self._opened = True
        return self

    async def close(self) -> None:
        self._opened = False

    async def _read(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def _write(self, fn, *args, **kwargs):
        async with self._write_lock:
            return await asyncio.to_thread(fn, *args, **kwargs)

    # ---------------------------------------------------------------- 设置

    def _get_settings_sync(self) -> dict[str, Any]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        result: dict[str, Any] = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (ValueError, TypeError):
                result[row["key"]] = row["value"]
        return result

    async def get_settings(self) -> dict[str, Any]:
        return await self._read(self._get_settings_sync)

    def _set_setting_sync(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, _dumps(value), utcnow()),
            )

    async def set_setting(self, key: str, value: Any) -> None:
        await self._write(self._set_setting_sync, key, value)

    async def set_settings(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            await self.set_setting(key, value)

    # ---------------------------------------------------------------- 账号

    async def save_account(
        self,
        mid: int,
        uname: str,
        cookies_enc: str,
        source: str = "qrcode",
        expires_hint: str = "",
    ) -> None:
        now = utcnow()

        def run() -> None:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO auth_accounts(mid, uname, cookies_enc, source, status, last_ok_at, expires_hint, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, 'ok', ?, ?, ?, ?) "
                    "ON CONFLICT(mid) DO UPDATE SET uname=excluded.uname, cookies_enc=excluded.cookies_enc, "
                    "source=excluded.source, status='ok', last_ok_at=excluded.last_ok_at, updated_at=excluded.updated_at",
                    (mid, uname, cookies_enc, source, now, expires_hint, now, now),
                )

        await self._write(run)

    async def latest_account(self) -> dict[str, Any] | None:
        def run() -> dict[str, Any] | None:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM auth_accounts WHERE status='ok' ORDER BY last_ok_at DESC, updated_at DESC LIMIT 1"
                ).fetchone()
            return dict(row) if row else None

        return await self._read(run)

    async def list_accounts(self) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT mid, uname, source, status, last_ok_at, expires_hint, created_at, updated_at "
                    "FROM auth_accounts ORDER BY updated_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._read(run)

    async def update_account_status(self, mid: int, status: str) -> None:
        def run() -> None:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE auth_accounts SET status=?, updated_at=? WHERE mid=?",
                    (status, utcnow(), mid),
                )

        await self._write(run)

    async def delete_account(self, mid: int) -> None:
        def run() -> None:
            with self._conn() as conn:
                conn.execute("DELETE FROM auth_accounts WHERE mid=?", (mid,))

        await self._write(run)

    # ---------------------------------------------------------------- 视频

    @staticmethod
    def _source_label(info: VideoInfo) -> str:
        """词条库的来源分类：B 站用 BV 号，其它站点用「作者 + 标题」。"""
        platform = (info.platform or "bilibili").lower()
        if platform == "bilibili":
            return (info.bvid or info.video_key).strip()[:64]
        owner = clean_text(info.owner_name).strip()
        title = clean_text(info.title).strip()
        if owner and title:
            return f"{owner}｜{title}"[:120]
        return (title or owner or info.video_key)[:120]

    @staticmethod
    def _video_url(info: VideoInfo) -> str:
        if info.url:
            return str(info.url)[:500]
        if (info.platform or "bilibili").lower() == "bilibili" and info.bvid:
            base = f"https://www.bilibili.com/video/{info.bvid}"
            if info.page_no > 1:
                base += f"?p={info.page_no}"
            return base
        return ""

    def _upsert_video_sync(self, info: VideoInfo, status: str = "pending") -> int:
        now = utcnow()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO videos(platform, video_id, bvid, aid, cid, title, owner_name, owner_mid, pubdate, duration, "
                "season_id, page_no, is_charge_blocked, status, source_label, url, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(platform, video_id) DO UPDATE SET "
                "aid=excluded.aid, cid=excluded.cid, title=excluded.title, owner_name=excluded.owner_name, "
                "owner_mid=excluded.owner_mid, pubdate=excluded.pubdate, duration=excluded.duration, "
                "season_id=excluded.season_id, page_no=excluded.page_no, is_charge_blocked=excluded.is_charge_blocked, "
                "source_label=excluded.source_label, url=excluded.url, updated_at=excluded.updated_at",
                (
                    info.platform or "bilibili",
                    info.video_key,
                    info.bvid,
                    info.aid,
                    info.cid,
                    clean_text(info.title),
                    clean_text(info.owner_name),
                    info.owner_mid,
                    info.pubdate,
                    info.duration,
                    info.season_id,
                    info.page_no,
                    1 if info.is_charge_blocked else 0,
                    status,
                    self._source_label(info),
                    self._video_url(info),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT id FROM videos WHERE platform=? AND video_id=?",
                (info.platform or "bilibili", info.video_key),
            ).fetchone()
            return int(row["id"])

    async def upsert_video(self, info: VideoInfo, status: str = "pending") -> int:
        return await self._write(self._upsert_video_sync, info, status)

    async def get_video(self, video_key: str, platform: str = "bilibili") -> dict[str, Any] | None:
        def run() -> dict[str, Any] | None:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM videos WHERE platform=? AND video_id=?", (platform, video_key)
                ).fetchone()
            return dict(row) if row else None

        return await self._read(run)

    async def set_video_status(
        self, video_key: str, status: str, error: str = "", platform: str = "bilibili"
    ) -> None:
        def run() -> None:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE videos SET status=?, last_error=?, updated_at=? WHERE platform=? AND video_id=?",
                    (status, error[:500], utcnow(), platform, video_key),
                )

        await self._write(run)

    async def list_videos(self, limit: int = 20, offset: int = 0, status: str | None = None) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            sql = "SELECT * FROM videos"
            params: list[Any] = []
            if status:
                sql += " WHERE status=?"
                params.append(status)
            sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

        return await self._read(run)

    # ---------------------------------------------------------------- 字幕

    def _add_transcript_sync(
        self,
        video_db_id: int,
        lang: str,
        provider: str,
        segments: Sequence[Segment],
        text_hash: str,
    ) -> bool:
        payload = _dumps([s.to_dict() for s in segments])
        chars = sum(len(s.text) for s in segments)
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO transcripts(video_id, lang, provider, segments_json, text_hash, char_count, fetched_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (video_db_id, lang, provider, payload, text_hash, chars, utcnow()),
            )
            return cursor.rowcount > 0

    async def add_transcript(
        self,
        video_db_id: int,
        lang: str,
        provider: str,
        segments: Sequence[Segment],
        text_hash: str,
    ) -> bool:
        return await self._write(self._add_transcript_sync, video_db_id, lang, provider, segments, text_hash)

    async def get_transcript(self, video_db_id: int) -> dict[str, Any] | None:
        def run() -> dict[str, Any] | None:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT * FROM transcripts WHERE video_id=? AND raw_deleted_at IS NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (video_db_id,),
                ).fetchone()
            return dict(row) if row else None

        return await self._read(run)

    def _purge_sync(self, days: int) -> int:
        if days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._conn() as conn:
            cursor = conn.execute(
                "UPDATE transcripts SET segments_json='[]', raw_deleted_at=? "
                "WHERE raw_deleted_at IS NULL AND fetched_at < ?",
                (utcnow(), cutoff),
            )
            return cursor.rowcount

    async def purge_raw_transcripts(self, days: int) -> int:
        return await self._write(self._purge_sync, days)

    # ---------------------------------------------------------------- 词条

    def _find_meme_sync(self, term: str) -> dict[str, Any] | None:
        norm = normalize_term(term)
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM memes WHERE norm_term=?", (norm,)).fetchone()
        return dict(row) if row else None

    async def find_meme(self, term: str) -> dict[str, Any] | None:
        return await self._read(self._find_meme_sync, term)

    def _upsert_meme_sync(
        self,
        candidate: MemeCandidate,
        origin: str,
        video_db_id: int | None,
        default_status: str,
        min_confidence: float,
    ) -> UpsertResult:
        term = clean_text(candidate.term)
        norm = normalize_term(term)
        if not is_meaningful_term(term):
            return UpsertResult(meme_id=0, created=False, conflict="词条名不合法")

        now = utcnow()
        aliases = _json_list(candidate.aliases)
        tags = _json_list(candidate.tags)
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM memes WHERE norm_term=?", (norm,)).fetchone()
            if row is None:
                cursor = conn.execute(
                    "INSERT INTO memes(term, norm_term, aliases, definition, usage, origin, examples, tags, confidence, heat, "
                    "status, sync_state, first_seen, last_seen, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, '[]', ?, ?, 1, ?, 'pending', ?, ?, ?, ?)",
                    (
                        term,
                        norm,
                        _dumps(aliases),
                        clean_text(candidate.definition),
                        clean_text(candidate.usage),
                        clean_text(origin),
                        _dumps(tags),
                        float(candidate.confidence),
                        default_status,
                        now,
                        now,
                        now,
                        now,
                    ),
                )
                meme_id = int(cursor.lastrowid)
                if video_db_id and candidate.quote:
                    conn.execute(
                        "INSERT OR IGNORE INTO meme_sources(meme_id, video_id, start_ms, quote, created_at) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (meme_id, video_db_id, int(candidate.start_ms), clean_text(candidate.quote), now),
                    )
                return UpsertResult(meme_id=meme_id, created=True)

            meme_id = int(row["id"])
            existing_aliases = _json_list(row["aliases"])
            existing_tags = _json_list(row["tags"])
            updates: dict[str, Any] = {}
            filled: list[str] = []

            if not row["definition"] and candidate.definition:
                updates["definition"] = clean_text(candidate.definition)
                filled.append("definition")
            if not row["usage"] and candidate.usage:
                updates["usage"] = clean_text(candidate.usage)
                filled.append("usage")
            merged_aliases = _merge_ci(existing_aliases, aliases)
            if merged_aliases != existing_aliases:
                updates["aliases"] = _dumps(merged_aliases)
                filled.append("aliases")
            merged_tags = list(dict.fromkeys([*existing_tags, *tags]))
            if merged_tags != existing_tags:
                updates["tags"] = _dumps(merged_tags)
                filled.append("tags")

            conflict = ""
            old_def = clean_text(row["definition"])
            new_def = clean_text(candidate.definition)
            if old_def and new_def and old_def != new_def:
                conflict = "定义与已有词条不一致"

            updates["heat"] = int(row["heat"]) + 1
            updates["last_seen"] = now
            updates["updated_at"] = now
            if float(candidate.confidence) > float(row["confidence"]):
                updates["confidence"] = float(candidate.confidence)
            if row["status"] == "draft" and float(candidate.confidence) >= min_confidence and not conflict:
                updates["status"] = default_status

            assignments = ", ".join(f"{key}=?" for key in updates)
            conn.execute(
                f"UPDATE memes SET {assignments} WHERE id=?",
                (*updates.values(), meme_id),
            )
            if video_db_id and candidate.quote:
                conn.execute(
                    "INSERT OR IGNORE INTO meme_sources(meme_id, video_id, start_ms, quote, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (meme_id, video_db_id, int(candidate.start_ms), clean_text(candidate.quote), now),
                )
            return UpsertResult(meme_id=meme_id, created=False, filled=filled, conflict=conflict)

    async def upsert_meme(
        self,
        candidate: MemeCandidate,
        origin: str = "",
        video_db_id: int | None = None,
        default_status: str = "draft",
        min_confidence: float = 0.6,
    ) -> UpsertResult:
        return await self._write(
            self._upsert_meme_sync, candidate, origin, video_db_id, default_status, min_confidence
        )

    async def get_meme(self, meme_id: int) -> dict[str, Any] | None:
        def run() -> dict[str, Any] | None:
            with self._conn() as conn:
                row = conn.execute("SELECT * FROM memes WHERE id=?", (meme_id,)).fetchone()
            return dict(row) if row else None

        return await self._read(run)

    async def list_memes(
        self,
        query: str = "",
        status: str | None = None,
        sync_state: str | None = None,
        source: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            sql = "SELECT * FROM memes"
            where: list[str] = []
            params: list[Any] = []
            if query.strip():
                q = query.strip()
                if self._fts and len(q) >= 3:
                    escaped = q.replace('"', '""')
                    where.append("id IN (SELECT rowid FROM memes_fts WHERE memes_fts MATCH ?)")
                    params.append(f'"{escaped}"')
                else:
                    like = f"%{q}%"
                    where.append("(term LIKE ? OR aliases LIKE ? OR definition LIKE ?)")
                    params.extend([like, like, like])
            if status:
                where.append("status=?")
                params.append(status)
            if sync_state:
                where.append("sync_state=?")
                params.append(sync_state)
            if source.strip():
                where.append("origin IN (SELECT url FROM videos WHERE source_label = ?)")
                params.append(source.strip())
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY heat DESC, updated_at DESC LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])
            with self._conn() as conn:
                try:
                    rows = conn.execute(sql, params).fetchall()
                except sqlite3.OperationalError:
                    # FTS 查询语法异常时降级 LIKE
                    like = f"%{query.strip()}%"
                    rows = conn.execute(
                        "SELECT * FROM memes WHERE term LIKE ? OR aliases LIKE ? OR definition LIKE ? "
                        "ORDER BY heat DESC, updated_at DESC LIMIT ? OFFSET ?",
                        (like, like, like, int(limit), int(offset)),
                    ).fetchall()
                return self._attach_source_labels(conn, rows)

        return await self._read(run)

    @staticmethod
    def _attach_source_labels(conn: sqlite3.Connection, rows) -> list[dict[str, Any]]:
        items = [dict(r) for r in rows]
        origins = {item.get("origin") for item in items if item.get("origin")}
        if not origins:
            return items
        marks = ",".join("?" for _ in origins)
        labels = {
            row["url"]: row["source_label"]
            for row in conn.execute(
                f"SELECT url, source_label FROM videos WHERE url IN ({marks})", tuple(origins)
            )
        }
        for item in items:
            item["source_label"] = labels.get(item.get("origin") or "", "")
        return items

    async def count_memes(self, status: str | None = None, source: str = "") -> int:
        def run() -> int:
            sql = "SELECT COUNT(*) AS c FROM memes"
            where: list[str] = []
            params: list[Any] = []
            if status:
                where.append("status=?")
                params.append(status)
            if source.strip():
                where.append("origin IN (SELECT url FROM videos WHERE source_label = ?)")
                params.append(source.strip())
            if where:
                sql += " WHERE " + " AND ".join(where)
            with self._conn() as conn:
                return int(conn.execute(sql, params).fetchone()["c"])

        return await self._read(run)

    async def match_memes_in_text(self, text: str, limit: int = 5) -> list[dict[str, Any]]:
        """找出「词条出现在这句话里」的已发布词条，按词长优先（长词更具体）。"""
        source = text or ""

        def run() -> list[dict[str, Any]]:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM memes WHERE status='published' AND length(term) >= 2 "
                    "ORDER BY length(term) DESC, heat DESC LIMIT 500"
                ).fetchall()
            normalized = normalize_term(source)
            hits: list[dict[str, Any]] = []
            for row in rows:
                term = normalize_term(str(row["term"] or ""))
                if term and matches_in_text(term, normalized):
                    hits.append(dict(row))
                else:
                    aliases = [normalize_term(a) for a in _json_list(row["aliases"])]
                    if any(
                        is_meaningful_term(a) and matches_in_text(a, normalized)
                        for a in aliases
                    ):
                        hits.append(dict(row))
                if len(hits) >= int(limit):
                    break
            return hits

        return await self._read(run)

    async def update_meme(self, meme_id: int, **fields: Any) -> bool:
        allowed = {
            "term",
            "aliases",
            "definition",
            "usage",
            "origin",
            "examples",
            "tags",
            "confidence",
            "status",
            "sync_state",
            "memewiki_slug",
            "sync_error",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        renamed = False
        if "term" in updates:
            updates["term"] = clean_text(str(updates["term"]))
            updates["norm_term"] = normalize_term(str(updates["term"]))
            renamed = True
        for key in ("aliases", "tags", "examples"):
            if key in updates and isinstance(updates[key], (list, tuple)):
                updates[key] = _dumps(list(updates[key]))
        if "status" in updates and updates["status"] not in ("draft", "published", "archived"):
            raise ValueError("非法状态")
        updates["updated_at"] = utcnow()
        force_pending = renamed and "sync_state" not in updates

        def run() -> bool:
            assignments = ", ".join(f"{key}=?" for key in updates)
            with self._conn() as conn:
                if renamed:
                    clash = conn.execute(
                        "SELECT term FROM memes WHERE norm_term=? AND id<>?",
                        (updates["norm_term"], meme_id),
                    ).fetchone()
                    if clash is not None:
                        raise ValueError(f"词条名已存在：{clash['term']}")
                    if force_pending:
                        conn.execute(
                            "UPDATE memes SET sync_state='pending' WHERE id=? AND sync_state<>'pending'",
                            (meme_id,),
                        )
                cursor = conn.execute(
                    f"UPDATE memes SET {assignments} WHERE id=?", (*updates.values(), meme_id)
                )
                return cursor.rowcount > 0

        return await self._write(run)

    async def delete_meme(self, meme_id: int) -> bool:
        def run() -> bool:
            with self._conn() as conn:
                cursor = conn.execute("DELETE FROM memes WHERE id=?", (meme_id,))
                return cursor.rowcount > 0

        return await self._write(run)

    async def pending_sync_memes(self, limit: int = 50) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM memes WHERE sync_state='pending' AND status='published' "
                    "ORDER BY updated_at ASC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._read(run)

    async def mark_synced(self, meme_ids: Iterable[int], state: str, slug: str = "", error: str = "") -> None:
        ids = [int(i) for i in meme_ids]
        if not ids:
            return

        def run() -> None:
            now = utcnow()
            with self._conn() as conn:
                for meme_id in ids:
                    conn.execute(
                        "UPDATE memes SET sync_state=?, memewiki_slug=?, sync_error=?, updated_at=? WHERE id=?",
                        (state, slug[:200], error[:300], now, meme_id),
                    )

        await self._write(run)

    async def add_source(self, meme_id: int, video_db_id: int, start_ms: int, quote: str) -> None:
        def run() -> None:
            with self._conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO meme_sources(meme_id, video_id, start_ms, quote, created_at) "
                    "VALUES(?, ?, ?, ?, ?)",
                    (meme_id, video_db_id, int(start_ms), clean_text(quote), utcnow()),
                )

        await self._write(run)

    async def list_sources(self, meme_id: int) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT s.*, v.title, v.bvid FROM meme_sources s "
                    "LEFT JOIN videos v ON v.id = s.video_id WHERE s.meme_id=? ORDER BY s.start_ms",
                    (meme_id,),
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._read(run)

    # ---------------------------------------------------------------- 审核

    async def add_review(
        self,
        kind: str,
        payload: dict[str, Any],
        reason: str = "",
        dedupe_key: str = "",
    ) -> int | None:
        def run() -> int | None:
            with self._conn() as conn:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO review_queue(kind, dedupe_key, payload, reason, status, created_at) "
                    "VALUES(?, ?, ?, ?, 'pending', ?)",
                    (kind, dedupe_key or None, _dumps(payload), reason[:500], utcnow()),
                )
                if cursor.rowcount == 0:
                    return None
                return int(cursor.lastrowid)

        return await self._write(run)

    async def list_reviews(self, status: str = "pending", limit: int = 50) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM review_queue WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, int(limit)),
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._read(run)

    async def resolve_review(self, review_id: int, action: str) -> bool:
        def run() -> bool:
            with self._conn() as conn:
                cursor = conn.execute(
                    "UPDATE review_queue SET status=?, resolved_at=? WHERE id=? AND status='pending'",
                    (action, utcnow(), review_id),
                )
                return cursor.rowcount > 0

        return await self._write(run)

    async def count_reviews(self) -> int:
        def run() -> int:
            with self._conn() as conn:
                return int(
                    conn.execute(
                        "SELECT COUNT(*) AS c FROM review_queue WHERE status='pending'"
                    ).fetchone()["c"]
                )

        return await self._read(run)

    # ---------------------------------------------------------------- 任务

    async def create_task(self, kind: str, target: str, payload: dict[str, Any] | None = None) -> str:
        task_id = uuid.uuid4().hex
        now = utcnow()

        def run() -> None:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO tasks(id, kind, target, status, progress, attempts, payload, created_at, updated_at) "
                    "VALUES(?, ?, ?, 'queued', 0, 0, ?, ?, ?)",
                    (task_id, kind, target[:500], _dumps(payload or {}), now, now),
                )

        await self._write(run)
        return task_id

    async def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {"status", "progress", "attempts", "last_error", "payload"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "payload" in updates and isinstance(updates["payload"], (dict, list)):
            updates["payload"] = _dumps(updates["payload"])
        if "progress" in updates:
            updates["progress"] = max(0, min(100, int(updates["progress"])))
        if "last_error" in updates:
            updates["last_error"] = str(updates["last_error"])[:500]
        updates["updated_at"] = utcnow()

        def run() -> None:
            assignments = ", ".join(f"{key}=?" for key in updates)
            with self._conn() as conn:
                conn.execute(f"UPDATE tasks SET {assignments} WHERE id=?", (*updates.values(), task_id))

        await self._write(run)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        def run() -> dict[str, Any] | None:
            with self._conn() as conn:
                row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return _decode_task(dict(row)) if row else None

        return await self._read(run)

    async def find_tasks_by_target(self, target: str, kind: str | None = None) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            sql = "SELECT * FROM tasks WHERE target=?"
            params: list[Any] = [target[:500]]
            if kind:
                sql += " AND kind=?"
                params.append(kind)
            sql += " ORDER BY created_at DESC"
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [_decode_task(dict(r)) for r in rows]

        return await self._read(run)

    async def delete_task(self, task_id: str) -> bool:
        def run() -> bool:
            with self._conn() as conn:
                cursor = conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
                return cursor.rowcount > 0

        return await self._write(run)

    async def list_sources(self, limit: int = 100) -> list[dict[str, Any]]:
        """按「来源分类」聚合：B 站为 BV 号，其它站点为作者｜标题。"""

        def run() -> list[dict[str, Any]]:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT v.platform, v.video_id, v.source_label, v.title, v.owner_name, v.url,"
                    "       COUNT(m.id) AS memes, MAX(v.updated_at) AS updated_at"
                    "  FROM videos v LEFT JOIN memes m ON m.origin = v.url"
                    " WHERE v.source_label <> ''"
                    " GROUP BY v.id"
                    " ORDER BY updated_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
            return [dict(r) for r in rows]

        return await self._read(run)

    async def list_tasks(
        self,
        limit: int = 20,
        status: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            sql = "SELECT * FROM tasks"
            params: list[Any] = []
            clauses: list[str] = []
            if status:
                clauses.append("status=?")
                params.append(status)
            if kind:
                clauses.append("kind=?")
                params.append(kind)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(int(limit))
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [_decode_task(dict(r)) for r in rows]

        return await self._read(run)

    async def fail_stale_tasks(self) -> int:
        """插件重启后把上次残留的 running 任务标记为失败。"""

        def run() -> int:
            with self._conn() as conn:
                cursor = conn.execute(
                    "UPDATE tasks SET status='failed', last_error='插件重启，任务中断', updated_at=? "
                    "WHERE status IN ('running', 'queued')",
                    (utcnow(),),
                )
                return cursor.rowcount

        return await self._write(run)

    # ---------------------------------------------------------------- 群黑话

    async def upsert_slang(
        self,
        scope: str,
        term: str,
        definition: str,
        aliases: Sequence[str] | None = None,
        created_by: str = "",
    ) -> int:
        norm = normalize_term(term)
        now = utcnow()

        def run() -> int:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO group_slangs(scope, term, norm_term, aliases, definition, enabled, created_by, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, 1, ?, ?, ?) "
                    "ON CONFLICT(scope, norm_term) DO UPDATE SET term=excluded.term, definition=excluded.definition, "
                    "aliases=excluded.aliases, enabled=1, updated_at=excluded.updated_at",
                    (scope, clean_text(term), norm, _dumps(list(aliases or [])), clean_text(definition), created_by, now, now),
                )
                row = conn.execute(
                    "SELECT id FROM group_slangs WHERE scope=? AND norm_term=?", (scope, norm)
                ).fetchone()
                return int(row["id"])

        return await self._write(run)

    async def list_slangs(self, scope: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            sql = "SELECT * FROM group_slangs"
            params: list[Any] = []
            if scope:
                sql += " WHERE scope=?"
                params.append(scope)
            sql += " ORDER BY updated_at DESC LIMIT ?"
            params.append(int(limit))
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

        return await self._read(run)

    async def find_slangs(self, scope: str, text: str, limit: int = 8) -> list[dict[str, Any]]:
        """在消息文本里找命中的群黑话（完整词出现在句子里）。"""

        def run() -> list[dict[str, Any]]:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM group_slangs WHERE scope=? AND enabled=1 ORDER BY length(term) DESC LIMIT 200",
                    (scope,),
                ).fetchall()
            normalized = normalize_term(text)
            hits: list[dict[str, Any]] = []
            for row in rows:
                candidates = [str(row["norm_term"] or "")]
                aliases = [normalize_term(a) for a in _json_list(row["aliases"])]
                candidates += [a for a in aliases if a and is_meaningful_term(a)]
                if any(matches_in_text(c, normalized) for c in candidates):
                    hits.append(dict(row))
                    if len(hits) >= int(limit):
                        break
            return hits

        return await self._read(run)

    async def delete_slang(self, scope: str, term: str) -> bool:
        norm = normalize_term(term)

        def run() -> bool:
            with self._conn() as conn:
                cursor = conn.execute(
                    "DELETE FROM group_slangs WHERE scope=? AND norm_term=?", (scope, norm)
                )
                return cursor.rowcount > 0

        return await self._write(run)

    # ---------------------------------------------------------------- 日志

    async def add_log(self, level: str, action: str, message: str, task_id: str = "") -> None:
        def run() -> None:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO sync_log(level, action, message, task_id, created_at) VALUES(?, ?, ?, ?, ?)",
                    (level, action[:100], str(message)[:1000], task_id, utcnow()),
                )

        await self._write(run)

    async def list_logs(self, limit: int = 50, level: str | None = None) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            sql = "SELECT * FROM sync_log"
            params: list[Any] = []
            if level:
                sql += " WHERE level=?"
                params.append(level)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(int(limit))
            with self._conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

        return await self._read(run)

    async def prune_logs(self, keep: int = 2000) -> int:
        def run() -> int:
            with self._conn() as conn:
                cursor = conn.execute(
                    "DELETE FROM sync_log WHERE id NOT IN (SELECT id FROM sync_log ORDER BY id DESC LIMIT ?)",
                    (int(keep),),
                )
                return cursor.rowcount

        return await self._write(run)

    # ---------------------------------------------------------------- 统计

    async def stats(self) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            with self._conn() as conn:
                def scalar(sql: str, params: Sequence[Any] = ()) -> int:
                    return int(conn.execute(sql, params).fetchone()[0])

                return {
                    "videos": scalar("SELECT COUNT(*) FROM videos"),
                    "videos_done": scalar("SELECT COUNT(*) FROM videos WHERE status='done'"),
                    "videos_failed": scalar("SELECT COUNT(*) FROM videos WHERE status='failed'"),
                    "transcripts": scalar("SELECT COUNT(*) FROM transcripts"),
                    "memes": scalar("SELECT COUNT(*) FROM memes"),
                    "memes_published": scalar("SELECT COUNT(*) FROM memes WHERE status='published'"),
                    "memes_draft": scalar("SELECT COUNT(*) FROM memes WHERE status='draft'"),
                    "memes_synced": scalar("SELECT COUNT(*) FROM memes WHERE sync_state='synced'"),
                    "memes_conflict": scalar("SELECT COUNT(*) FROM memes WHERE sync_state='conflict'"),
                    "reviews": scalar("SELECT COUNT(*) FROM review_queue WHERE status='pending'"),
                    "tasks_running": scalar("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running')"),
                    "slangs": scalar("SELECT COUNT(*) FROM group_slangs"),
                    "accounts": scalar("SELECT COUNT(*) FROM auth_accounts WHERE status='ok'"),
                    "fts": 1 if self._fts else 0,
                    "db_path": str(self.db_path),
                }

        return await self._read(run)

    def sync_result(self, **kwargs: Any) -> SyncResult:
        return SyncResult(**kwargs)
