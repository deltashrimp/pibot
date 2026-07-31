"""Persistence layer for PiBot.

Defines the abstract :class:`Persistence` interface and its SQLite
implementation :class:`SQLitePersistence`. All writes go through a
pending-buffer that is flushed periodically in a single transaction,
which keeps the number of DB round-trips low and the data consistent.
"""

import asyncio
import json
import logging
import sqlite3
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Coroutine

import aiosqlite

logger = logging.getLogger(__name__)

FLUSH_INTERVAL = 60  # 1 minute
CHAT_DATA_CACHE_TTL = 300  # 5 minutes
USER_CONFIG_CACHE_TTL = 604800  # 7 days
MAINTENANCE_OPTIMIZE_INTERVAL = 86400  # once a day
MAINTENANCE_VACUUM_INTERVAL = 604800  # once a week
HISTORY_RETENTION = 86400  # 1 day
HISTORY_LOAD_LIMIT = 100
PENDING_HISTORY_LIMIT = 10000

DB_RETRY_MAX_ATTEMPTS = 3
DB_RETRY_BASE_DELAY = 0.1


@dataclass
class ChatData:
    """State for a single chat.

    Persistent fields (``ranks``, ``ignored_until``, ``config``) are
    loaded from the database; transient fields (``message_ids``,
    ``spam_tracker``, ...) live only in memory.
    """

    ranks: dict[int, int] = field(default_factory=dict)
    ignored_until: dict[int, float] = field(default_factory=dict)
    config: dict[str, str] = field(default_factory=dict)
    message_ids: deque[int] = field(default_factory=deque)
    spam_tracker: dict[int, list[float]] = field(default_factory=dict)
    spam_warned: dict[int, float] = field(default_factory=dict)
    trigger_spam: dict[int, dict[str, list[float]]] = field(default_factory=dict)

    def drop_expired(self, now: float) -> bool:
        """Remove expired ignore entries from this chat's cache.

        Returns ``True`` if anything was removed (caller may invalidate
        the loaded-at timestamp so the DB is reloaded next access).
        """
        expired = [uid for uid, expiry in self.ignored_until.items() if now >= expiry]
        for user_id in expired:
            del self.ignored_until[user_id]
        return bool(expired)


class Persistence(ABC):
    """Abstract persistence interface used by services and the bot."""

    @abstractmethod
    async def init_db(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    def start_periodic_flush(self) -> None: ...

    @abstractmethod
    async def flush(self) -> None: ...

    @abstractmethod
    def schedule_task(self, coro: Coroutine[Any, Any, Any]) -> None: ...

    # Global bans

    @abstractmethod
    async def load_global_bans(self) -> None: ...

    @property
    @abstractmethod
    def banned_users(self) -> set[int]: ...

    @abstractmethod
    async def add_global_ban(self, user_id: int) -> None: ...

    @abstractmethod
    async def remove_global_ban(self, user_id: int) -> None: ...

    @abstractmethod
    def is_user_banned(self, user_id: int) -> bool: ...

    # Known chats

    @abstractmethod
    async def load_known_chats(self) -> None: ...

    @property
    @abstractmethod
    def known_chats(self) -> set[int]: ...

    @abstractmethod
    async def add_known_chat(self, chat_id: int) -> None: ...

    # Username map

    @abstractmethod
    async def load_username_map(self) -> None: ...

    @property
    @abstractmethod
    def username_map(self) -> dict[str, int]: ...

    @abstractmethod
    async def update_username_map(self, username: str, user_id: int) -> None: ...

    # Bot config

    @abstractmethod
    async def get_bot_config(self, key: str, default: str = "") -> str: ...

    @abstractmethod
    async def set_bot_config(self, key: str, value: str) -> None: ...

    # Dev IDs

    @property
    @abstractmethod
    def dev_ids(self) -> set[int]: ...

    # Chat data

    @abstractmethod
    async def get_chat_data(self, chat_id: int) -> ChatData: ...

    @abstractmethod
    def get_chat_data_sync(self, chat_id: int) -> ChatData: ...

    @abstractmethod
    async def get_chat_ranks(self, chat_id: int) -> dict[int, int]: ...

    @abstractmethod
    async def set_chat_rank(self, chat_id: int, user_id: int, rank: int) -> None: ...

    # Chat mutes

    @abstractmethod
    async def add_chat_mute(self, chat_id: int, user_id: int, until: int) -> None: ...

    @abstractmethod
    async def remove_chat_mute(self, chat_id: int, user_id: int) -> None: ...

    # Chat ignored

    @abstractmethod
    async def set_chat_ignored(self, chat_id: int, user_id: int, until: float) -> None: ...

    @abstractmethod
    async def remove_chat_ignored(self, chat_id: int, user_id: int) -> None: ...

    @abstractmethod
    async def get_chat_ignored(self, chat_id: int) -> dict[int, float]: ...

    # Chat config

    @abstractmethod
    async def get_chat_config(self, chat_id: int, key: str, default: str = "") -> str: ...

    @abstractmethod
    async def set_chat_config(self, chat_id: int, key: str, value: str) -> None: ...

    # User config

    @abstractmethod
    async def get_user_config(self, user_id: int, key: str, default: str = "") -> str: ...

    @abstractmethod
    async def set_user_config(self, user_id: int, key: str, value: str) -> None: ...

    # Chat history (AI)

    @abstractmethod
    async def buffer_chat_history(
        self, chat_id: int, user_id: int, role: str, content: str
    ) -> None: ...

    @abstractmethod
    async def load_chat_history(
        self, chat_id: int, limit: int = HISTORY_LOAD_LIMIT
    ) -> list[dict[str, str]]: ...

    # Clear all

    @abstractmethod
    async def clear_all(self) -> None: ...


class SQLitePersistence(Persistence):
    """SQLite-backed persistence implementation using aiosqlite."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._flush_lock = asyncio.Lock()

        self._banned_users: set[int] = set()
        self._known_chats: set[int] = set()
        self._username_map: dict[str, int] = {}
        self._bot_config: dict[str, str] = {}
        self._chat_data_cache: dict[int, ChatData] = {}
        self._chat_data_loaded_at: dict[int, float] = {}
        self._user_config_cache: dict[int, dict[str, str]] = {}
        self._user_config_loaded_at: dict[int, float] = {}
        self._dev_ids: set[int] = set()

        self._pending_known_chats: set[int] = set()
        self._pending_username_map: dict[str, int] = {}
        self._pending_bot_config: dict[str, str] = {}
        self._pending_chat_config: dict[tuple[int, str], str] = {}
        self._pending_user_config: dict[tuple[int, str], str] = {}
        self._pending_ranks: dict[tuple[int, int], int] = {}
        self._pending_mutes: dict[tuple[int, int], int] = {}
        self._pending_mutes_deletes: set[tuple[int, int]] = set()
        self._pending_ignored: dict[tuple[int, int], int] = {}
        self._pending_ignored_deletes: set[tuple[int, int]] = set()
        self._pending_history: list[tuple[int, int, str, str, float]] = []

        self._last_optimize: float = 0.0
        self._last_vacuum: float = 0.0

    # ── Low-level helpers with retry ─────────────────────────────

    async def _execute(
        self, sql: str, params: tuple[Any, ...] | list[Any] = ()
    ) -> aiosqlite.Cursor:
        """Execute a single statement, retrying on SQLite lock/busy."""
        assert self._db is not None
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(DB_RETRY_MAX_ATTEMPTS):
            try:
                return await self._db.execute(sql, params)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                last_error = e
                if attempt < DB_RETRY_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(DB_RETRY_BASE_DELAY * (2**attempt))
        raise last_error  # type: ignore[misc]

    async def _executemany(
        self, sql: str, params: list[Any]
    ) -> aiosqlite.Cursor:
        """Execute a batched statement, retrying on SQLite lock/busy."""
        assert self._db is not None
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(DB_RETRY_MAX_ATTEMPTS):
            try:
                return await self._db.executemany(sql, params)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                last_error = e
                if attempt < DB_RETRY_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(DB_RETRY_BASE_DELAY * (2**attempt))
        raise last_error  # type: ignore[misc]

    # ── Lifecycle ────────────────────────────────────────────────

    async def init_db(self) -> None:
        """Open the database, apply PRAGMAs, create tables and load caches."""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.execute("PRAGMA cache_size=10000")
        await self._create_tables()
        await self._drop_old_persistence()
        await self._backfill_known_chats()
        await self._db.commit()
        await self._load_startup_data()

    async def _create_tables(self) -> None:
        assert self._db is not None
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS global_bans (
                user_id INTEGER PRIMARY KEY
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS chat_ranks (
                chat_id INTEGER NOT NULL REFERENCES known_chats(chat_id)
                    ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS chat_mutes (
                chat_id INTEGER NOT NULL REFERENCES known_chats(chat_id)
                    ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                until INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS chat_ignored (
                chat_id INTEGER NOT NULL REFERENCES known_chats(chat_id)
                    ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                until INTEGER NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS username_map (
                username TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS known_chats (
                chat_id INTEGER PRIMARY KEY
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS chat_config (
                chat_id INTEGER NOT NULL REFERENCES known_chats(chat_id)
                    ON DELETE CASCADE,
                key TEXT,
                value TEXT NOT NULL,
                PRIMARY KEY (chat_id, key)
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS user_config (
                user_id INTEGER,
                key TEXT,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS dev_ids (
                user_id INTEGER PRIMARY KEY
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                chat_id INTEGER,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp REAL
            )
        """)
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_ranks_chat ON chat_ranks(chat_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_mutes_chat ON chat_mutes(chat_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_ignored_chat ON chat_ignored(chat_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_mutes_until ON chat_mutes(until)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_ignored_until ON chat_ignored(until)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_username_map_uid ON username_map(user_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_history_chat "
            "ON chat_history(chat_id, timestamp)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_history_ts ON chat_history(timestamp)"
        )

    async def _backfill_known_chats(self) -> None:
        """Ensure every chat referenced by child tables exists in known_chats.

        Needed because older databases may contain orphan rows before the
        foreign-key constraints were introduced.
        """
        assert self._db is not None
        await self._db.execute("""
            INSERT OR IGNORE INTO known_chats (chat_id)
            SELECT chat_id FROM chat_ranks
            UNION
            SELECT chat_id FROM chat_ignored
            UNION
            SELECT chat_id FROM chat_config
        """)

    async def _drop_old_persistence(self) -> None:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='persistence'"
        )
        if await cursor.fetchone():
            logger.info("Dropping old persistence table")
            await self._db.execute("DROP TABLE persistence")

    async def _load_startup_data(self) -> None:
        await self.load_global_bans()
        await self.load_known_chats()
        await self.load_username_map()
        await self._load_bot_config()
        await self._load_dev_ids()

    async def close(self) -> None:
        """Cancel periodic tasks, wait for background writes, flush, close."""
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        if self._db:
            try:
                await self.flush()
            except Exception as e:
                logger.warning("[close] flush failed: %s", e)
            await self._db.close()
            self._db = None

    def schedule_task(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run ``coro`` in the background and keep a reference to it.

        Kept in ``_background_tasks`` so it is not garbage-collected and
        so ``close()`` can await all pending work on shutdown.
        """
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def start_periodic_flush(self) -> None:
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._periodic_flush_loop())

    async def _periodic_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            try:
                await self.flush()
            except Exception as e:
                logger.warning("[periodic flush] %s", e)
            try:
                await self._periodic_maintenance()
            except Exception as e:
                logger.warning("[maintenance] %s", e)

    # ── Flushing ─────────────────────────────────────────────────

    async def flush(self) -> None:
        """Write all pending changes to the DB in a single transaction.

        Pending buffers are only cleared after a successful commit, so a
        failure never loses data (the batch is retried on the next flush).
        """
        if self._db is None:
            return
        async with self._flush_lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        """Write all pending changes; caller must hold ``_flush_lock``."""
        if self._db is None:
            return

        snapshot_known = set(self._pending_known_chats)
        snapshot_username = dict(self._pending_username_map)
        snapshot_bot_config = dict(self._pending_bot_config)
        snapshot_chat_config = dict(self._pending_chat_config)
        snapshot_user_config = dict(self._pending_user_config)
        snapshot_ranks = dict(self._pending_ranks)
        snapshot_mutes_deletes = set(self._pending_mutes_deletes)
        snapshot_mutes = dict(self._pending_mutes)
        snapshot_ignored_deletes = set(self._pending_ignored_deletes)
        snapshot_ignored = dict(self._pending_ignored)
        history_count = len(self._pending_history)
        snapshot_history = self._pending_history[:history_count]

        batches: list[tuple[str, list[Any]]] = []
        if snapshot_known:
            batches.append(
                (
                    "INSERT OR IGNORE INTO known_chats (chat_id) VALUES (?)",
                    [(chat_id,) for chat_id in snapshot_known],
                )
            )
        if snapshot_username:
            batches.append(
                (
                    "INSERT OR REPLACE INTO username_map (username, user_id) "
                    "VALUES (?, ?)",
                    list(snapshot_username.items()),
                )
            )
        if snapshot_bot_config:
            batches.append(
                (
                    "INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)",
                    list(snapshot_bot_config.items()),
                )
            )
        if snapshot_chat_config:
            batches.append(
                (
                    "INSERT OR REPLACE INTO chat_config (chat_id, key, value) "
                    "VALUES (?, ?, ?)",
                    [
                        (cid, key, value)
                        for (cid, key), value in snapshot_chat_config.items()
                    ],
                )
            )
        if snapshot_user_config:
            batches.append(
                (
                    "INSERT OR REPLACE INTO user_config (user_id, key, value) "
                    "VALUES (?, ?, ?)",
                    [
                        (uid, key, value)
                        for (uid, key), value in snapshot_user_config.items()
                    ],
                )
            )
        if snapshot_ranks:
            batches.append(
                (
                    "INSERT OR REPLACE INTO chat_ranks (chat_id, user_id, rank) "
                    "VALUES (?, ?, ?)",
                    [
                        (cid, uid, rank)
                        for (cid, uid), rank in snapshot_ranks.items()
                    ],
                )
            )
        if snapshot_mutes_deletes:
            batches.append(
                (
                    "DELETE FROM chat_mutes WHERE chat_id = ? AND user_id = ?",
                    list(snapshot_mutes_deletes),
                )
            )
        if snapshot_mutes:
            batches.append(
                (
                    "INSERT OR REPLACE INTO chat_mutes (chat_id, user_id, until) "
                    "VALUES (?, ?, ?)",
                    [
                        (cid, uid, until)
                        for (cid, uid), until in snapshot_mutes.items()
                    ],
                )
            )
        if snapshot_ignored_deletes:
            batches.append(
                (
                    "DELETE FROM chat_ignored WHERE chat_id = ? AND user_id = ?",
                    list(snapshot_ignored_deletes),
                )
            )
        if snapshot_ignored:
            batches.append(
                (
                    "INSERT OR REPLACE INTO chat_ignored (chat_id, user_id, until) "
                    "VALUES (?, ?, ?)",
                    [
                        (cid, uid, until)
                        for (cid, uid), until in snapshot_ignored.items()
                    ],
                )
            )
        if snapshot_history:
            batches.append(
                (
                    "INSERT INTO chat_history "
                    "(chat_id, user_id, role, content, timestamp) "
                    "VALUES (?, ?, ?, ?, ?)",
                    snapshot_history,
                )
            )

        if not batches:
            return

        try:
            for sql, params in batches:
                await self._executemany(sql, params)
            await self._db.commit()
        except Exception:
            try:
                await self._db.rollback()
            except Exception:
                pass
            raise

        for cid in snapshot_known:
            self._pending_known_chats.discard(cid)
        for uname in snapshot_username:
            self._pending_username_map.pop(uname, None)
        for bot_cfg_key in snapshot_bot_config:
            self._pending_bot_config.pop(bot_cfg_key, None)
        for chat_cfg_key in snapshot_chat_config:
            self._pending_chat_config.pop(chat_cfg_key, None)
        for user_cfg_key in snapshot_user_config:
            self._pending_user_config.pop(user_cfg_key, None)
        for rank_key in snapshot_ranks:
            self._pending_ranks.pop(rank_key, None)
        for mute_key in snapshot_mutes_deletes:
            self._pending_mutes_deletes.discard(mute_key)
        for mute_key in snapshot_mutes:
            self._pending_mutes.pop(mute_key, None)
        for ignore_key in snapshot_ignored_deletes:
            self._pending_ignored_deletes.discard(ignore_key)
        for ignore_key in snapshot_ignored:
            self._pending_ignored.pop(ignore_key, None)
        del self._pending_history[:history_count]

    # ── Maintenance ──────────────────────────────────────────────

    async def _periodic_maintenance(self) -> None:
        assert self._db is not None
        now = time.time()
        await self._expire_old_rows()
        await self._db.commit()
        self._purge_stale_user_config_cache(now)
        if now - self._last_optimize >= MAINTENANCE_OPTIMIZE_INTERVAL:
            await self._db.execute("PRAGMA optimize")
            await self._db.commit()
            self._last_optimize = now
        if now - self._last_vacuum >= MAINTENANCE_VACUUM_INTERVAL:
            self._last_vacuum = now
            await self._vacuum()

    async def _vacuum(self) -> None:
        """Run VACUUM on a dedicated connection.

        VACUUM can lock the database for a long time, so it runs on its
        own connection with a short busy timeout to avoid blocking writes.
        """
        try:
            conn = await aiosqlite.connect(self.db_path)
            try:
                await conn.execute("PRAGMA busy_timeout=1000")
                await conn.execute("VACUUM")
                await conn.commit()
                logger.info("Database VACUUM completed")
            finally:
                await conn.close()
        except Exception as e:
            logger.warning("[maintenance] VACUUM failed: %s", e)

    async def _expire_old_rows(self) -> None:
        """Delete expired rows and keep pending buffers + caches consistent."""
        assert self._db is not None
        now = time.time()
        await self._execute("DELETE FROM chat_mutes WHERE until < ?", (int(now),))
        await self._execute("DELETE FROM chat_ignored WHERE until < ?", (int(now),))
        await self._execute(
            "DELETE FROM chat_history WHERE timestamp < ?", (now - HISTORY_RETENTION,)
        )
        self._prune_pending_expired(now)
        self._invalidate_expired_cache(now)

    def _prune_pending_expired(self, now: float) -> None:
        """Drop pending mute/ignore entries that have already expired."""
        for key in [k for k, until in self._pending_mutes.items() if until < now]:
            del self._pending_mutes[key]
        for key in [k for k, until in self._pending_ignored.items() if until < now]:
            del self._pending_ignored[key]

    def _invalidate_expired_cache(self, now: float) -> None:
        """Drop expired ignores from the chat data cache and force reload."""
        for chat_id, data in self._chat_data_cache.items():
            if data.drop_expired(now):
                self._chat_data_loaded_at[chat_id] = 0.0

    # ── Global bans (critical, committed immediately) ────────────

    async def load_global_bans(self) -> None:
        assert self._db is not None
        self._banned_users = set()
        async with self._db.execute("SELECT user_id FROM global_bans") as cur:
            async for row in cur:
                self._banned_users.add(row[0])

    @property
    def banned_users(self) -> set[int]:
        return self._banned_users

    async def add_global_ban(self, user_id: int) -> None:
        assert self._db is not None
        self._banned_users.add(user_id)
        await self._execute(
            "INSERT OR IGNORE INTO global_bans (user_id) VALUES (?)", (user_id,)
        )
        await self._db.commit()

    async def remove_global_ban(self, user_id: int) -> None:
        assert self._db is not None
        self._banned_users.discard(user_id)
        await self._execute(
            "DELETE FROM global_bans WHERE user_id = ?", (user_id,)
        )
        await self._db.commit()

    def is_user_banned(self, user_id: int) -> bool:
        return user_id in self._banned_users

    # ── Known chats ──────────────────────────────────────────────

    async def load_known_chats(self) -> None:
        assert self._db is not None
        self._known_chats = set()
        async with self._db.execute("SELECT chat_id FROM known_chats") as cur:
            async for row in cur:
                self._known_chats.add(row[0])

    @property
    def known_chats(self) -> set[int]:
        return self._known_chats

    async def add_known_chat(self, chat_id: int) -> None:
        if chat_id in self._known_chats:
            return
        self._known_chats.add(chat_id)
        self._pending_known_chats.add(chat_id)

    # ── Username map ─────────────────────────────────────────────

    async def load_username_map(self) -> None:
        assert self._db is not None
        self._username_map = {}
        async with self._db.execute("SELECT username, user_id FROM username_map") as cur:
            async for row in cur:
                self._username_map[row[0]] = row[1]

    @property
    def username_map(self) -> dict[str, int]:
        return self._username_map

    async def update_username_map(self, username: str, user_id: int) -> None:
        self._username_map[username] = user_id
        self._pending_username_map[username] = user_id

    # ── Bot config ───────────────────────────────────────────────

    async def _load_bot_config(self) -> None:
        assert self._db is not None
        self._bot_config = {}
        async with self._db.execute("SELECT key, value FROM bot_config") as cur:
            async for row in cur:
                self._bot_config[row[0]] = row[1]

    async def get_bot_config(self, key: str, default: str = "") -> str:
        return self._bot_config.get(key, default)

    async def set_bot_config(self, key: str, value: str) -> None:
        self._bot_config[key] = value
        self._pending_bot_config[key] = value

    # ── Dev IDs ─────────────────────────────────────────────────

    async def _load_dev_ids(self) -> None:
        assert self._db is not None
        self._dev_ids = set()
        async with self._db.execute("SELECT user_id FROM dev_ids") as cur:
            async for row in cur:
                self._dev_ids.add(row[0])
        if 934151958 not in self._dev_ids:
            self._dev_ids.add(934151958)
            await self._execute(
                "INSERT OR IGNORE INTO dev_ids (user_id) VALUES (?)", (934151958,)
            )
            await self._db.commit()

    @property
    def dev_ids(self) -> set[int]:
        return self._dev_ids

    # ── Chat data (ranks + ignored + config) ─────────────────────

    async def get_chat_data(self, chat_id: int) -> ChatData:
        now = time.time()
        if chat_id in self._chat_data_cache:
            loaded_at = self._chat_data_loaded_at.get(chat_id, 0.0)
            if now - loaded_at <= CHAT_DATA_CACHE_TTL:
                data = self._chat_data_cache[chat_id]
                self._chat_data_loaded_at[chat_id] = now
                return data
            data = await self._reload_chat_data(chat_id)
        else:
            data = await self._load_chat_persistent_fields(chat_id)
        self._chat_data_cache[chat_id] = data
        self._chat_data_loaded_at[chat_id] = now
        return data

    async def _load_chat_persistent_fields(self, chat_id: int) -> ChatData:
        """Load ranks, ignored users and config for a chat in one query."""
        assert self._db is not None
        data = ChatData()
        now = time.time()
        sql = """
            SELECT
                (SELECT json_group_array(json_array(user_id, rank))
                 FROM chat_ranks WHERE chat_id = ?) AS ranks,
                (SELECT json_group_array(json_array(user_id, until))
                 FROM chat_ignored WHERE chat_id = ? AND until > ?) AS ignored,
                (SELECT json_group_array(json_array(key, value))
                 FROM chat_config WHERE chat_id = ?) AS config
        """
        async with self._db.execute(sql, (chat_id, chat_id, int(now), chat_id)) as cur:
            row = await cur.fetchone()
        if row:
            if row[0]:
                for user_id, rank in json.loads(row[0]):
                    data.ranks[int(user_id)] = int(rank)
            if row[1]:
                for user_id, until in json.loads(row[1]):
                    data.ignored_until[int(user_id)] = float(until)
            if row[2]:
                for key, value in json.loads(row[2]):
                    data.config[key] = value
        self._merge_pending_into(chat_id, data)
        return data

    async def _reload_chat_data(self, chat_id: int) -> ChatData:
        old = self._chat_data_cache.get(chat_id)
        data = await self._load_chat_persistent_fields(chat_id)
        if old is not None:
            data.message_ids = old.message_ids
            data.spam_tracker = old.spam_tracker
            data.spam_warned = old.spam_warned
            data.trigger_spam = old.trigger_spam
        return data

    def _merge_pending_into(self, chat_id: int, data: ChatData) -> None:
        for (cid, user_id), rank in self._pending_ranks.items():
            if cid == chat_id:
                data.ranks[user_id] = rank
        for (cid, user_id), until in self._pending_ignored.items():
            if cid == chat_id:
                data.ignored_until[user_id] = float(until)
        for cid, user_id in self._pending_ignored_deletes:
            if cid == chat_id:
                data.ignored_until.pop(user_id, None)
        for (cid, key), value in self._pending_chat_config.items():
            if cid == chat_id:
                data.config[key] = value

    def get_chat_data_sync(self, chat_id: int) -> ChatData:
        if chat_id not in self._chat_data_cache:
            self._chat_data_cache[chat_id] = ChatData()
        return self._chat_data_cache[chat_id]

    # ── Chat ranks ───────────────────────────────────────────────

    async def get_chat_ranks(self, chat_id: int) -> dict[int, int]:
        data = await self.get_chat_data(chat_id)
        return data.ranks

    async def set_chat_rank(self, chat_id: int, user_id: int, rank: int) -> None:
        data = await self.get_chat_data(chat_id)
        data.ranks[user_id] = rank
        self._pending_ranks[(chat_id, user_id)] = rank

    # ── Chat mutes ───────────────────────────────────────────────

    async def add_chat_mute(self, chat_id: int, user_id: int, until: int) -> None:
        self._pending_mutes[(chat_id, user_id)] = until
        self._pending_mutes_deletes.discard((chat_id, user_id))

    async def remove_chat_mute(self, chat_id: int, user_id: int) -> None:
        self._pending_mutes.pop((chat_id, user_id), None)
        self._pending_mutes_deletes.add((chat_id, user_id))

    # ── Chat ignored ─────────────────────────────────────────────

    async def set_chat_ignored(self, chat_id: int, user_id: int, until: float) -> None:
        data = await self.get_chat_data(chat_id)
        data.ignored_until[user_id] = until
        self._pending_ignored[(chat_id, user_id)] = int(until)
        self._pending_ignored_deletes.discard((chat_id, user_id))

    async def remove_chat_ignored(self, chat_id: int, user_id: int) -> None:
        data = await self.get_chat_data(chat_id)
        data.ignored_until.pop(user_id, None)
        self._pending_ignored.pop((chat_id, user_id), None)
        self._pending_ignored_deletes.add((chat_id, user_id))

    async def get_chat_ignored(self, chat_id: int) -> dict[int, float]:
        data = await self.get_chat_data(chat_id)
        now = time.time()
        return {
            user_id: expiry
            for user_id, expiry in data.ignored_until.items()
            if now < expiry
        }

    # ── Chat config ──────────────────────────────────────────────

    async def get_chat_config(self, chat_id: int, key: str, default: str = "") -> str:
        data = await self.get_chat_data(chat_id)
        return data.config.get(key, default)

    async def set_chat_config(self, chat_id: int, key: str, value: str) -> None:
        data = await self.get_chat_data(chat_id)
        data.config[key] = value
        self._pending_chat_config[(chat_id, key)] = value

    # ── User config ──────────────────────────────────────────────

    async def get_user_config(self, user_id: int, key: str, default: str = "") -> str:
        assert self._db is not None
        if user_id not in self._user_config_cache:
            config: dict[str, str] = {}
            async with self._db.execute(
                "SELECT key, value FROM user_config WHERE user_id = ?", (user_id,)
            ) as cur:
                async for row in cur:
                    config[row[0]] = row[1]
            self._user_config_cache[user_id] = config
        self._user_config_loaded_at[user_id] = time.time()
        return self._user_config_cache[user_id].get(key, default)

    def _purge_stale_user_config_cache(self, now: float) -> None:
        """Drop user config caches that have not been touched recently."""
        stale = [
            uid
            for uid, loaded_at in self._user_config_loaded_at.items()
            if now - loaded_at > USER_CONFIG_CACHE_TTL
        ]
        for user_id in stale:
            self._user_config_cache.pop(user_id, None)
            self._user_config_loaded_at.pop(user_id, None)

    async def set_user_config(self, user_id: int, key: str, value: str) -> None:
        assert self._db is not None
        if user_id not in self._user_config_cache:
            await self.get_user_config(user_id, key)
        self._user_config_cache[user_id][key] = value
        self._pending_user_config[(user_id, key)] = value

    # ── Chat history (AI) ────────────────────────────────────────

    async def buffer_chat_history(
        self, chat_id: int, user_id: int, role: str, content: str
    ) -> None:
        async with self._flush_lock:
            self._pending_history.append(
                (chat_id, user_id, role, content, time.time())
            )
            if len(self._pending_history) > PENDING_HISTORY_LIMIT:
                await self._flush_locked()

    async def load_chat_history(
        self, chat_id: int, limit: int = HISTORY_LOAD_LIMIT
    ) -> list[dict[str, str]]:
        assert self._db is not None
        rows: list[tuple[str, str]] = []
        async with self._db.execute(
            "SELECT role, content FROM chat_history WHERE chat_id = ? "
            "ORDER BY timestamp DESC, rowid DESC LIMIT ?",
            (chat_id, limit),
        ) as cur:
            async for row in cur:
                rows.append((row[0], row[1]))
        return [{"role": role, "content": content} for role, content in reversed(rows)]

    # ── Clear all ────────────────────────────────────────────────

    async def clear_all(self) -> None:
        assert self._db is not None
        await self._db.execute("DELETE FROM global_bans")
        await self._db.execute("DELETE FROM chat_ranks")
        await self._db.execute("DELETE FROM chat_mutes")
        await self._db.execute("DELETE FROM chat_ignored")
        await self._db.execute("DELETE FROM username_map")
        await self._db.execute("DELETE FROM known_chats")
        await self._db.execute("DELETE FROM bot_config")
        await self._db.execute("DELETE FROM chat_config")
        await self._db.execute("DELETE FROM user_config")
        await self._db.execute("DELETE FROM dev_ids")
        await self._db.execute("DELETE FROM chat_history")
        await self._db.commit()
        self._banned_users.clear()
        self._known_chats.clear()
        self._username_map.clear()
        self._bot_config.clear()
        self._chat_data_cache.clear()
        self._chat_data_loaded_at.clear()
        self._user_config_cache.clear()
        self._user_config_loaded_at.clear()
        self._dev_ids.clear()
        self._pending_known_chats.clear()
        self._pending_username_map.clear()
        self._pending_bot_config.clear()
        self._pending_chat_config.clear()
        self._pending_user_config.clear()
        self._pending_ranks.clear()
        self._pending_mutes.clear()
        self._pending_mutes_deletes.clear()
        self._pending_ignored.clear()
        self._pending_ignored_deletes.clear()
        self._pending_history.clear()
