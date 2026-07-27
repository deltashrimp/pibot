import asyncio
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

FLUSH_INTERVAL = 300  # 5 minutes


class SQLitePersistence:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._flush_task: asyncio.Task[None] | None = None

        self._banned_users: set[int] = set()
        self._known_chats: set[int] = set()
        self._username_map: dict[str, int] = {}
        self._bot_config: dict[str, str] = {}
        self._chat_data_cache: dict[int, dict[str, Any]] = {}

    async def init_db(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._create_tables()
        await self._drop_old_persistence()
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
                chat_id INTEGER,
                user_id INTEGER,
                rank INTEGER,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS chat_mutes (
                chat_id INTEGER,
                user_id INTEGER,
                until INTEGER,
                PRIMARY KEY (chat_id, user_id)
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS chat_ignored (
                chat_id INTEGER,
                user_id INTEGER,
                until INTEGER,
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
                chat_id INTEGER,
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
            "CREATE INDEX IF NOT EXISTS idx_username_map_uid ON username_map(user_id)"
        )

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

    async def close(self) -> None:
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        if self._db:
            await self._db.commit()
            await self._db.close()
            self._db = None

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

    async def flush(self) -> None:
        if self._db:
            await self._db.commit()

    # ── Global bans ──────────────────────────────────────────────

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
        await self._db.execute(
            "INSERT OR IGNORE INTO global_bans (user_id) VALUES (?)", (user_id,)
        )
        await self._db.commit()

    async def remove_global_ban(self, user_id: int) -> None:
        assert self._db is not None
        self._banned_users.discard(user_id)
        await self._db.execute("DELETE FROM global_bans WHERE user_id = ?", (user_id,))
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
        assert self._db is not None
        self._known_chats.add(chat_id)
        await self._db.execute(
            "INSERT OR IGNORE INTO known_chats (chat_id) VALUES (?)", (chat_id,)
        )
        await self._db.commit()

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
        assert self._db is not None
        self._username_map[username] = user_id
        await self._db.execute(
            "INSERT OR REPLACE INTO username_map (username, user_id) VALUES (?, ?)",
            (username, user_id),
        )
        await self._db.commit()

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
        assert self._db is not None
        self._bot_config[key] = value
        await self._db.execute(
            "INSERT OR REPLACE INTO bot_config (key, value) VALUES (?, ?)", (key, value)
        )
        await self._db.commit()

    # ── Chat data (ranks + transient) ────────────────────────────

    async def get_chat_data(self, chat_id: int) -> dict[str, Any]:
        if chat_id not in self._chat_data_cache:
            await self._load_chat_persistent_fields(chat_id)
        data = self._chat_data_cache[chat_id]
        data.setdefault("message_ids", deque())
        data.setdefault("spam_tracker", {})
        data.setdefault("spam_warned", {})
        data.setdefault("trigger_spam", {})
        return data

    async def _load_chat_persistent_fields(self, chat_id: int) -> None:
        assert self._db is not None
        data: dict[str, Any] = {}
        now = time.time()

        ranks = {}
        async with self._db.execute(
            "SELECT user_id, rank FROM chat_ranks WHERE chat_id = ?", (chat_id,)
        ) as cur:
            async for row in cur:
                ranks[row[0]] = row[1]
        if ranks:
            data["ranks"] = ranks

        ignored: dict[int, float] = {}
        async with self._db.execute(
            "SELECT user_id, until FROM chat_ignored WHERE chat_id = ?", (chat_id,)
        ) as cur:
            async for row in cur:
                if now < row[1]:
                    ignored[row[0]] = float(row[1])
        if ignored:
            data["ignored_until"] = ignored

        self._chat_data_cache[chat_id] = data

    def get_chat_data_sync(self, chat_id: int) -> dict[str, Any]:
        if chat_id not in self._chat_data_cache:
            self._chat_data_cache[chat_id] = {}
        return self._chat_data_cache[chat_id]

    # ── Chat ranks ───────────────────────────────────────────────

    async def get_chat_ranks(self, chat_id: int) -> dict[int, int]:
        assert self._db is not None
        ranks: dict[int, int] = {}
        async with self._db.execute(
            "SELECT user_id, rank FROM chat_ranks WHERE chat_id = ?", (chat_id,)
        ) as cur:
            async for row in cur:
                ranks[row[0]] = row[1]
        return ranks

    async def set_chat_rank(self, chat_id: int, user_id: int, rank: int) -> None:
        assert self._db is not None
        if chat_id in self._chat_data_cache:
            ranks = self._chat_data_cache[chat_id].setdefault("ranks", {})
            ranks[user_id] = rank
        await self._db.execute(
            "INSERT OR REPLACE INTO chat_ranks (chat_id, user_id, rank) VALUES (?, ?, ?)",
            (chat_id, user_id, rank),
        )
        await self._db.commit()

    # ── Chat mutes ───────────────────────────────────────────────

    async def add_chat_mute(self, chat_id: int, user_id: int, until: int) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO chat_mutes (chat_id, user_id, until) VALUES (?, ?, ?)",
            (chat_id, user_id, until),
        )
        await self._db.commit()

    async def remove_chat_mute(self, chat_id: int, user_id: int) -> None:
        assert self._db is not None
        await self._db.execute(
            "DELETE FROM chat_mutes WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        await self._db.commit()

    # ── Chat ignored ─────────────────────────────────────────────

    async def set_chat_ignored(self, chat_id: int, user_id: int, until: float) -> None:
        assert self._db is not None
        if chat_id in self._chat_data_cache:
            ignored = self._chat_data_cache[chat_id].setdefault("ignored_until", {})
            ignored[user_id] = until
        await self._db.execute(
            "INSERT OR REPLACE INTO chat_ignored (chat_id, user_id, until) VALUES (?, ?, ?)",
            (chat_id, user_id, int(until)),
        )
        await self._db.commit()

    async def remove_chat_ignored(self, chat_id: int, user_id: int) -> None:
        assert self._db is not None
        if chat_id in self._chat_data_cache:
            ignored = self._chat_data_cache[chat_id].get("ignored_until", {})
            ignored.pop(user_id, None)
        await self._db.execute(
            "DELETE FROM chat_ignored WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        await self._db.commit()

    async def get_chat_ignored(self, chat_id: int) -> dict[int, float]:
        assert self._db is not None
        result: dict[int, float] = {}
        now = time.time()
        async with self._db.execute(
            "SELECT user_id, until FROM chat_ignored WHERE chat_id = ?", (chat_id,)
        ) as cur:
            async for row in cur:
                if now < row[1]:
                    result[row[0]] = float(row[1])
        return result

    # ── Chat config ──────────────────────────────────────────────

    async def get_chat_config(self, chat_id: int, key: str, default: str = "") -> str:
        assert self._db is not None
        async with self._db.execute(
            "SELECT value FROM chat_config WHERE chat_id = ? AND key = ?",
            (chat_id, key),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default

    async def set_chat_config(self, chat_id: int, key: str, value: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO chat_config (chat_id, key, value) VALUES (?, ?, ?)",
            (chat_id, key, value),
        )
        await self._db.commit()

    # ── User config ──────────────────────────────────────────────

    async def get_user_config(self, user_id: int, key: str, default: str = "") -> str:
        assert self._db is not None
        async with self._db.execute(
            "SELECT value FROM user_config WHERE user_id = ? AND key = ?",
            (user_id, key),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default

    async def set_user_config(self, user_id: int, key: str, value: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT OR REPLACE INTO user_config (user_id, key, value) VALUES (?, ?, ?)",
            (user_id, key, value),
        )
        await self._db.commit()

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
        await self._db.commit()
        self._banned_users.clear()
        self._known_chats.clear()
        self._username_map.clear()
        self._bot_config.clear()
        self._chat_data_cache.clear()
