import asyncio
import logging
import time

from telegram.ext import CallbackContext

from storage import (
    TRIGGER_SPAM_WINDOW,
    TRIGGER_SPAM_LIMIT,
    TRIGGER_SPAM_MUTE,
)

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, max_calls: int = 5, period: float = 1.0) -> None:
        self.max_calls = max_calls
        self.period = period
        self.tokens = float(max_calls)
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(
                self.max_calls, self.tokens + elapsed * (self.max_calls / self.period)
            )
            self.last_refill = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True
            return False


rate_limiter = RateLimiter(max_calls=5, period=1.0)


def is_user_ignored(context: CallbackContext, user_id: int) -> bool:
    ignored = context.chat_data.get("ignored_until", {})
    expiry = ignored.get(user_id)
    if expiry is None:
        return False
    if time.time() >= expiry:
        del ignored[user_id]
        return False
    return True


def track_trigger_spam(context: CallbackContext, user_id: int, phrase: str) -> bool:
    now = time.time()
    trackers = context.chat_data.setdefault("trigger_spam", {})
    user_tracker = trackers.setdefault(user_id, {})
    timestamps = user_tracker.setdefault(phrase, [])
    cutoff = now - TRIGGER_SPAM_WINDOW
    timestamps[:] = [t for t in timestamps if t > cutoff]

    if not timestamps:
        user_tracker.pop(phrase, None)
        if not user_tracker:
            trackers.pop(user_id, None)
        return False

    timestamps.append(now)
    if len(timestamps) > TRIGGER_SPAM_LIMIT:
        ignored = context.chat_data.setdefault("ignored_until", {})
        ignored[user_id] = now + TRIGGER_SPAM_MUTE
        return True
    return False


llm_rate_limiters: dict[int, RateLimiter] = {}
llm_global_limiter = RateLimiter(max_calls=30, period=60.0)


def get_llm_rate_limiter(chat_id: int) -> RateLimiter:
    if chat_id not in llm_rate_limiters:
        llm_rate_limiters[chat_id] = RateLimiter(max_calls=3, period=60.0)
    return llm_rate_limiters[chat_id]
