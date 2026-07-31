"""Chat-level business logic: filtering, anti-spam, ranks, mute/ignore.

The module-level helpers (``is_user_ignored``, ``get_user_rank``, ...)
are pure functions operating on :class:`persistence.ChatData`; the
:class:`ChatService` class wires them together with persistence and the
filter manager for use by middleware and commands.
"""

import logging
import string
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from aiogram.enums import ChatMemberStatus, MessageEntityType
from aiogram.types import Chat, Message, User

from constants import (
    ANTISPAM_MAX_MESSAGE_AGE,
    ANTISPAM_MSG_LIMIT,
    ANTISPAM_MUTE_DURATION,
    ANTISPAM_MUTE_THRESHOLD,
    ANTISPAM_WINDOW,
    PIBOT_PREFIX,
    RANK_ADMIN,
    RANK_MEMBER,
    RANK_OWNER,
    TRIGGER_SPAM_LIMIT,
    TRIGGER_SPAM_MUTE,
    TRIGGER_SPAM_WINDOW,
)
from filtering import FilterManager
from persistence import ChatData, Persistence
from utils import NO_PERMISSIONS, RateLimiter, get_mention

if TYPE_CHECKING:
    from pibot import PiBot

logger = logging.getLogger(__name__)

STRIP_PUNCT = str.maketrans("", "", string.punctuation)


def is_user_ignored(chat_data: ChatData, user_id: int) -> bool:
    """Check whether ``user_id`` is currently ignored in this chat."""
    expiry = chat_data.ignored_until.get(user_id)
    if expiry is None:
        return False
    if time.time() >= expiry:
        del chat_data.ignored_until[user_id]
        return False
    return True


async def get_user_rank(chat: Chat, chat_data: ChatData, user_id: int) -> int:
    """Return the effective rank of ``user_id`` in ``chat``.

    Telegram creator/administrator status takes precedence over the
    stored bot rank.
    """
    try:
        member = await chat.get_member(user_id)
        if member.status == ChatMemberStatus.CREATOR:
            return RANK_OWNER
        if member.status == ChatMemberStatus.ADMINISTRATOR:
            return RANK_ADMIN
    except Exception:
        pass
    return chat_data.ranks.get(user_id, RANK_MEMBER)


async def target_immune_to_mkb(
    chat: Chat, chat_data: ChatData, target_user_id: int
) -> bool:
    """Return True if the target is an owner/admin and may not be punished."""
    rank = await get_user_rank(chat, chat_data, target_user_id)
    return rank <= RANK_ADMIN


async def _resolve_by_username(
    username: str, bot_data: dict, chat: Chat
) -> Optional[User]:
    username_map = bot_data.get("username_map", {})
    if username in username_map:
        user_id = username_map[username]
        try:
            member = await chat.get_member(user_id)
            return member.user
        except Exception:
            pass
    return None


async def resolve_user(
    message: Message, bot_data: dict, chat: Chat, params: str
) -> Optional[User]:
    """Resolve the target user of a moderation command.

    Priority: replied-to user, text mention entity, @username, then raw
    ``params`` matched against the username map.
    """
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user

    if message.entities and message.text:
        text = message.text
        for entity in message.entities:
            if entity.type == MessageEntityType.TEXT_MENTION:
                return entity.user
            elif entity.type == MessageEntityType.MENTION:
                start = entity.offset
                end = entity.offset + entity.length
                mention_text = text[start:end]
                username = mention_text[1:].lower()
                result = await _resolve_by_username(username, bot_data, chat)
                if result:
                    return result

    if params:
        username = params.strip().lstrip("@").lower()
        result = await _resolve_by_username(username, bot_data, chat)
        if result:
            return result

    return None


class ChatService:
    """Coordinates filtering, anti-spam and phrase/RP responses."""

    def __init__(
        self,
        pibot: "PiBot",
        persistence: Persistence,
        filter_manager: FilterManager,
        phrases: dict[str, str],
        rp_commands: dict[str, str],
        rate_limiter: RateLimiter,
    ) -> None:
        self.pibot = pibot
        self.persistence = persistence
        self.filter_manager = filter_manager
        self.phrases = phrases
        self.rp_commands = rp_commands
        self.rate_limiter = rate_limiter

    def track_trigger_spam(
        self, chat_data: ChatData, chat_id: int, user_id: int, phrase: str
    ) -> bool:
        """Track repeated trigger-phrase usage; mute the user if excessive."""
        now = time.time()
        trackers = chat_data.trigger_spam
        user_tracker = trackers.setdefault(user_id, {})
        timestamps = user_tracker.setdefault(phrase, [])
        cutoff = now - TRIGGER_SPAM_WINDOW
        timestamps[:] = [t for t in timestamps if t > cutoff]
        timestamps.append(now)
        if len(timestamps) > TRIGGER_SPAM_LIMIT:
            chat_data.ignored_until[user_id] = now + TRIGGER_SPAM_MUTE
            self.persistence.schedule_task(
                self.persistence.set_chat_ignored(
                    chat_id, user_id, now + TRIGGER_SPAM_MUTE
                )
            )
            return True
        return False

    async def check_filter(self, message: Message, chat_data: ChatData) -> bool:
        """Filter the message; delete and mute if it matches.

        Returns ``True`` if the message may continue to be handled, or
        ``False`` if it was dropped (matched the filter).
        """
        if not message.text:
            return True
        user = message.from_user
        assert user is not None
        bot = message.bot
        assert bot is not None
        result = self.filter_manager.check(
            message.text, message.date, user.id, message.chat.id
        )
        if not result.matched:
            return True
        try:
            await message.delete()
        except Exception as e:
            logger.warning("[Filter] не удалось удалить сообщение: %s", e)
        if result.should_mute:
            try:
                kwargs: dict = {}
                if result.mute_until is not None:
                    kwargs["until_date"] = result.mute_until
                await bot.restrict_chat_member(
                    message.chat.id,
                    user.id,
                    permissions=NO_PERMISSIONS,
                    **kwargs,
                )
            except Exception as e:
                logger.warning("[Filter] не удалось замутить: %s", e)
        return False

    async def check_spam(self, message: Message, chat_data: ChatData) -> bool:
        """Anti-flood check. Returns ``True`` to continue, ``False`` to drop.

        Warns on mild flooding and mutes the user once the threshold is
        crossed.
        """
        if message.date:
            msg_date = message.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            msg_age = (datetime.now(timezone.utc) - msg_date).total_seconds()
            if msg_age > ANTISPAM_MAX_MESSAGE_AGE:
                return True

        if time.time() - self.pibot.start_time < 15:
            return True

        user = message.from_user
        assert user is not None
        bot = message.bot
        assert bot is not None

        now = time.time()
        ts = chat_data.spam_tracker.setdefault(user.id, [])
        cutoff = now - ANTISPAM_WINDOW
        ts[:] = [t for t in ts if t > cutoff]
        ts.append(now)

        if len(ts) <= ANTISPAM_MSG_LIMIT:
            return True

        if message.text:
            lower = message.text.lower().strip().translate(STRIP_PUNCT)
            if lower in self.phrases or lower.startswith(PIBOT_PREFIX + "инфа"):
                return True
            if message.reply_to_message and lower in self.rp_commands:
                return True

        spammer_name = get_mention(user)

        if len(ts) <= ANTISPAM_MUTE_THRESHOLD:
            warned = chat_data.spam_warned
            if user.id not in warned:
                warned[user.id] = now
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=f"⚠️ {spammer_name}, пожалуйста, не флуди!",
                )
            return False

        try:
            await bot.restrict_chat_member(
                message.chat.id,
                user.id,
                permissions=NO_PERMISSIONS,
                until_date=int(now + ANTISPAM_MUTE_DURATION),
            )
        except Exception as e:
            logger.warning("[AntiSpam] не получилось замутить: %s", e)
            return True

        await bot.send_message(
            chat_id=message.chat.id,
            text=f"✅️ Я замутил {spammer_name} за спам.",
        )
        return False

    async def handle_phrase(
        self, message: Message, chat_data: ChatData, lower_text: str
    ) -> bool:
        """Respond to a known trigger phrase. Returns ``True`` if handled."""
        if lower_text not in self.phrases:
            return False

        user = message.from_user
        assert user is not None

        if self.track_trigger_spam(
            chat_data, message.chat.id, user.id, lower_text
        ):
            await self.pibot.safe_reply(message, "Ой всё", disable_notification=True)
            return True
        if not await self.rate_limiter.acquire():
            return True

        response = self.phrases[lower_text]
        mention = get_mention(user)
        response = response.replace("{mention}", mention)
        await self.pibot.safe_reply(message, response, disable_notification=True)
        return True

    async def handle_rp(
        self, message: Message, chat_data: ChatData, lower_text: str
    ) -> bool:
        """Handle a roleplay command used as a reply. ``True`` if handled."""
        if not (message.reply_to_message and message.reply_to_message.from_user):
            return False
        if lower_text not in self.rp_commands:
            return False

        user = message.from_user
        assert user is not None

        if self.track_trigger_spam(
            chat_data, message.chat.id, user.id, lower_text
        ):
            await self.pibot.safe_reply(message, "Ой всё", disable_notification=True)
            return True
        if not await self.rate_limiter.acquire():
            return True

        user1 = get_mention(user)
        user2 = get_mention(message.reply_to_message.from_user)
        response = (
            self.rp_commands[lower_text]
            .replace("{mention1}", user1)
            .replace("{mention2}", user2)
        )
        await self.pibot.safe_reply(message, response, disable_notification=True)
        return True
