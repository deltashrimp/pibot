"""Small shared helpers: rate limiting, permissions and formatting."""

import asyncio
import time

from aiogram.types import ChatPermissions, User

from constants import MAX_MESSAGE_LENGTH

NO_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_other_messages=False,
    can_send_polls=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
)

ALL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_other_messages=True,
    can_send_polls=True,
    can_add_web_page_previews=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
)


class RateLimiter:
    """Simple sliding-window rate limiter using a per-instance lock."""

    def __init__(self, max_calls: int = 5, period: float = 1.0) -> None:
        self.max_calls = max_calls
        self.period = period
        self.timestamps: list[float] = []
        self.lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self.lock:
            now = time.monotonic()
            cutoff = now - self.period
            self.timestamps = [t for t in self.timestamps if t > cutoff]
            if len(self.timestamps) < self.max_calls:
                self.timestamps.append(now)
                return True
            return False


def pluralize_minutes(n: int) -> str:
    """Return the correct Russian plural form for ``n`` minutes."""
    n = abs(n)
    if 11 <= n % 100 <= 19:
        return "минут"
    last = n % 10
    if last == 1:
        return "минуту"
    if 2 <= last <= 4:
        return "минуты"
    return "минут"


def get_mention(user: User) -> str:
    """Return a mentionable display string for a user."""
    return f"@{user.username}" if user.username else (user.first_name or "User")


def chunk_text(
    text: str, limit: int = MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters.

    Paragraphs are preserved where possible; paragraphs longer than the
    limit are hard-split (preferring word boundaries).
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for para in text.split("\n"):
        line = para
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            while len(line) > limit:
                cut = line.rfind(" ", 0, limit)
                if cut < limit // 2:
                    cut = limit
                chunks.append(line[:cut].rstrip())
                line = line[cut:].lstrip()
            current = line
            continue
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks
