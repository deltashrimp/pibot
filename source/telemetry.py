import asyncio
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import aiofiles

from constants import (
    TELEMETRY_BACKUP_COUNT,
    TELEMETRY_MAX_BYTES,
    TELEMETRY_WRITE_BASE_DELAY,
    TELEMETRY_WRITE_MAX_ATTEMPTS,
)

logger = logging.getLogger(__name__)


class Telemetry:
    """Async telemetry store with size-based rotation.

    Records are written to ``path`` as JSON. When the file exceeds
    ``TELEMETRY_MAX_BYTES`` it is rotated to ``path.N`` (oldest first),
    keeping up to ``TELEMETRY_BACKUP_COUNT`` backups.
    """

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
            try:
                await self._write()
            except OSError as e:
                logger.warning("[telemetry] write failed after retries: %s", e)

    def _rotate(self) -> None:
        if not self.path.exists():
            return
        if self.path.stat().st_size <= TELEMETRY_MAX_BYTES:
            return
        for i in range(TELEMETRY_BACKUP_COUNT, 0, -1):
            dst = self.path.with_name(f"{self.path.name}.{i}")
            src = self.path.with_name(f"{self.path.name}.{i - 1}")
            if i == 1:
                src = self.path
            if dst.exists():
                dst.unlink()
            if src.exists():
                shutil.move(str(src), str(dst))
        self.path.write_text("[]", encoding="utf-8")

    async def _write(self) -> None:
        self._rotate()
        serialized = json.dumps(self._records, ensure_ascii=False, indent=2)
        last_error: OSError | None = None
        for attempt in range(TELEMETRY_WRITE_MAX_ATTEMPTS):
            try:
                async with aiofiles.open(self.path, "w", encoding="utf-8") as f:
                    await f.write(serialized)
                return
            except OSError as e:
                last_error = e
                if attempt < TELEMETRY_WRITE_MAX_ATTEMPTS - 1:
                    delay = TELEMETRY_WRITE_BASE_DELAY * (2**attempt)
                    await asyncio.sleep(delay)
        raise last_error  # type: ignore[misc]
