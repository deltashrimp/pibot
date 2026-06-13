import asyncio
import json
import sqlite3
from collections import deque
from pathlib import Path
from typing import Any


class SetEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, set):
            return {"__type__": "set", "__items__": list(obj)}
        if isinstance(obj, deque):
            return {"__type__": "deque", "__items__": list(obj)}
        return str(obj)


def _object_hook(dct: dict) -> Any:
    if "__type__" in dct:
        t = dct["__type__"]
        if t == "set":
            return set(dct["__items__"])
        if t == "deque":
            return deque(dct["__items__"])
    return dct


def _serialize(obj: Any) -> str:
    return json.dumps(obj, cls=SetEncoder, ensure_ascii=False)


def _deserialize(data: str) -> Any:
    return json.loads(data, object_hook=_object_hook)


class SQLitePersistence:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._bot_data: dict[str, Any] = {}
        self._chat_data: dict[int, dict[str, Any]] = {}
        self._user_data: dict[int, dict[str, Any]] = {}
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS persistence (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)

    @property
    def bot_data(self) -> dict[str, Any]:
        return self._bot_data

    @property
    def chat_data(self) -> dict[int, dict[str, Any]]:
        return self._chat_data

    @property
    def user_data(self) -> dict[int, dict[str, Any]]:
        return self._user_data

    async def load_all(self) -> None:
        def _sync() -> dict[str, Any]:
            result: dict[str, Any] = {}
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT key, value FROM persistence"
                ).fetchall()
                for key, value in rows:
                    result[key] = _deserialize(value)
            return result

        data = await asyncio.to_thread(_sync)

        self._bot_data = data.get("bot_data", {})

        self._chat_data = {}
        for key, value in data.items():
            if key.startswith("chat_data_"):
                try:
                    cid = int(key[len("chat_data_"):])
                    self._chat_data[cid] = value
                except (ValueError, TypeError):
                    pass
            elif key == "chat_data" and isinstance(value, dict):
                for k, v in value.items():
                    try:
                        self._chat_data[int(k)] = v
                    except (ValueError, TypeError):
                        pass

        self._user_data = {}
        for key, value in data.items():
            if key.startswith("user_data_"):
                try:
                    uid = int(key[len("user_data_"):])
                    self._user_data[uid] = value
                except (ValueError, TypeError):
                    pass
            elif key == "user_data" and isinstance(value, dict):
                for k, v in value.items():
                    try:
                        self._user_data[int(k)] = v
                    except (ValueError, TypeError):
                        pass

    async def flush(self) -> None:
        await self._store("bot_data", self._bot_data)
        for cid, data in self._chat_data.items():
            await self._store(f"chat_data_{cid}", data)
        for uid, data in self._user_data.items():
            await self._store(f"user_data_{uid}", data)

    async def clear_all(self) -> None:
        def _sync() -> None:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM persistence")

        await asyncio.to_thread(_sync)
        self._bot_data.clear()
        self._chat_data.clear()
        self._user_data.clear()

    async def _store(self, key: str, value: Any) -> None:
        serialized = _serialize(value)

        def _sync() -> None:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO persistence (key, value) VALUES (?, ?)",
                    (key, serialized),
                )

        await asyncio.to_thread(_sync)
