import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any


class Telemetry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._records: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    self._records = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._records = []

    async def record(self, entry: dict[str, Any]) -> None:
        async with self._lock:
            entry["timestamp"] = datetime.now().strftime("%d/%m/%Y, %H:%M")
            self._records.append(entry)
            self._write()

    def _write(self) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self._records, f, ensure_ascii=False, indent=2)
