import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

FILTERS: list[str] = [
    "заработай в телеграм",
    "пассивный доход",
    "вложение от",
    "гарантия дохода",
    "нужны люди для",
    "ссылка в био",
    "пиши в лс",
    "удалённая работа",
    "доход в день",
    "вы плывете",
    "консультация бесплатно",
]

_MAX_MESSAGE_AGE = 120


@dataclass
class FilterResult:
    matched: bool
    pattern: str | None = None
    should_delete: bool = False
    should_mute: bool = False
    mute_until: int | None = None


class FilterManager:
    def __init__(
        self,
        patterns: list[str] | None = None,
        mute_duration: int = 60,
        max_message_age: int = _MAX_MESSAGE_AGE,
    ) -> None:
        self._mute_duration = mute_duration
        self._max_message_age = max_message_age
        self._patterns = patterns or FILTERS
        self._regex: re.Pattern[str] | None = None
        self._compile()
        self._total_checked = 0
        self._total_matched = 0
        self._total_muted = 0

    def _compile(self) -> None:
        if not self._patterns:
            self._regex = None
            return
        escaped = [re.escape(p) for p in self._patterns]
        self._regex = re.compile("|".join(escaped), re.IGNORECASE)

    def check(
        self,
        text: str,
        message_date: datetime | None,
        user_id: int,
        chat_id: int,
    ) -> FilterResult:
        if message_date is not None:
            dt = message_date
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - dt).total_seconds() > self._max_message_age:
                return FilterResult(matched=False)

        if self._regex is None:
            return FilterResult(matched=False)

        m = self._regex.search(text)
        if m is None:
            self._total_checked += 1
            return FilterResult(matched=False)

        matched_pattern = m.group(0)

        self._total_checked += 1
        self._total_matched += 1
        self._total_muted += 1

        logger.info(
            "[Filter] chat=%d user=%d matched \"%s\"",
            chat_id,
            user_id,
            matched_pattern,
        )

        return FilterResult(
            matched=True,
            pattern=matched_pattern,
            should_delete=True,
            should_mute=True,
        )

    def get_stats(self) -> dict[str, int]:
        return {
            "total_checked": self._total_checked,
            "total_matched": self._total_matched,
            "total_muted": self._total_muted,
        }
