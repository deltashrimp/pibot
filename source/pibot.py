"""PiBot: Telegram bot entry point, middleware and lifecycle.

Business logic lives in dedicated services (``ChatService``,
``UserService``, ``AIService``, ``CommandRouter``); this module wires
them together and keeps only initialisation, middleware, event
dispatching and startup/shutdown.
"""

import asyncio
import fcntl
import json
import logging
import os
import string
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, TelegramObject
from dotenv import load_dotenv

from ai_service import AIService
from anti_raid import handle_raid_protection, on_chat_member
from chat_service import ChatService, get_user_rank, is_user_ignored
from command_router import CommandRouter
from constants import (
    ANTISPAM_MUTE_DURATION,
    ANTISPAM_WINDOW,
    MAX_MESSAGE_AGE,
    MAX_MESSAGE_LENGTH,
    MAX_TRACKED_MESSAGES,
    PIBOT_PREFIX,
    RANK_ADMIN,
    TRIGGER_SPAM_WINDOW,
)
from filtering import FILTERS, FilterManager
from greeter import cmd_start
from logging_settings import setup_logging
from persistence import ChatData, SQLitePersistence
from telemetry import Telemetry
from user_service import UserService
from utils import RateLimiter, chunk_text

logger = logging.getLogger(__name__)

BASE = Path(__file__).parent.parent
PHRASES_PATH = BASE / "bot-data" / "phrases.json"
LINKS_PATH = BASE / "bot-data" / "links.json"
RP_COMMANDS_PATH = BASE / "bot-data" / "rp-phrases.json"
DEV_IDS_PATH = BASE / "env" / "dev-ids.json"
PERSONALITY_PATH = BASE / "bot-data" / "personality.md"
TELEMETRY_PATH = BASE / "logs" / "telemetry.json"

PID_FILE = BASE / "pibot.pid"

STRIP_PUNCT = str.maketrans("", "", string.punctuation)


def load_phrases() -> dict[str, str]:
    if PHRASES_PATH.exists():
        with open(PHRASES_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_links() -> dict[str, str]:
    if LINKS_PATH.exists():
        with open(LINKS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_rp_commands() -> dict[str, str]:
    if RP_COMMANDS_PATH.exists():
        with open(RP_COMMANDS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_dev_ids() -> set[int]:
    if DEV_IDS_PATH.exists():
        with open(DEV_IDS_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def acquire_pid_lock() -> int:
    try:
        fd = os.open(str(PID_FILE), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        logger.debug("PID lock acquired (pid=%d)", os.getpid())
        return fd
    except (IOError, OSError):
        try:
            existing = PID_FILE.read_text().strip()
            logger.critical(
                "Другой экземпляр бота уже запущен (PID %s, файл %s)",
                existing,
                PID_FILE,
            )
        except Exception:
            logger.critical("Другой экземпляр бота уже запущен (%s)", PID_FILE)
        sys.exit(1)


def release_pid_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        PID_FILE.unlink(missing_ok=True)
        logger.debug("PID lock released")
    except Exception as e:
        logger.warning("Ошибка при освобождении PID блокировки: %s", e)


class PiBotMiddleware(BaseMiddleware):
    """Prepares per-message context and applies chat-level protection."""

    def __init__(self, pibot: "PiBot") -> None:
        self.pibot = pibot

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Any],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)
        persistence = self.pibot.persistence
        chat_id = event.chat.id

        chat_data = await persistence.get_chat_data(chat_id)

        ids = chat_data.message_ids
        ids.append(event.message_id)
        while len(ids) > MAX_TRACKED_MESSAGES:
            ids.popleft()

        await persistence.add_known_chat(chat_id)

        user = event.from_user
        if user and user.username:
            await persistence.update_username_map(user.username.lower(), user.id)

        bot_data = {
            "banned_users": list(persistence.banned_users),
            "known_chats": persistence.known_chats,
            "username_map": persistence.username_map,
            "llm_provider": await persistence.get_bot_config("llm_provider", ""),
            "persistence": persistence,
        }

        data["chat_data"] = chat_data
        data["bot_data"] = bot_data
        data["_persistence"] = persistence

        if not user or user.is_bot:
            return await handler(event, data)

        if event.text and event.chat.type in ("group", "supergroup"):
            self.pibot.ai_service.add_group_message(chat_id, event)

        if event.chat.type not in ("group", "supergroup"):
            return await handler(event, data)

        if user.id in persistence.banned_users:
            return await handler(event, data)

        user_rank = await get_user_rank(event.chat, chat_data, user.id)
        if user_rank <= RANK_ADMIN:
            return await handler(event, data)

        if not await self.pibot.chat_service.check_filter(event, chat_data):
            return None

        if not await self.pibot.chat_service.check_spam(event, chat_data):
            return None

        return await handler(event, data)


class PiBot:
    """Bot container: wiring, middleware, lifecycle and event dispatch."""

    def __init__(
        self, token: str, groq_key: str = "", openrouter_key: str = ""
    ) -> None:
        self.phrases = load_phrases()
        self.links = load_links()
        self.rp_commands = load_rp_commands()
        self.dev_ids = load_dev_ids()
        self.filter_manager = FilterManager(FILTERS, mute_duration=60)

        self.msg_locks: dict[int, asyncio.Lock] = {}
        self.bot_message_ids: dict[int, deque[int]] = {}
        self.replied_to_ids: dict[int, set[int]] = {}
        self.pending_writes: dict[int, str] = {}
        self.rate_limiter = RateLimiter(max_calls=5, period=1.0)
        self.telemetry = Telemetry(TELEMETRY_PATH)

        self.persistence = SQLitePersistence(
            db_path=Path(__file__).parent / "bot_data.db"
        )

        self.bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
        )
        self.dp = Dispatcher()

        self.personality = self._load_personality()
        if not self.personality:
            logger.warning("AI: personality.md не найден, используется заглушка")
            self.personality = (
                "Ты — Пибот, 25-летняя томбой. Отвечай коротко и по делу."
            )

        self.chat_service = ChatService(
            self,
            self.persistence,
            self.filter_manager,
            self.phrases,
            self.rp_commands,
            self.rate_limiter,
        )
        self.user_service = UserService(self.persistence)
        self.ai_service = AIService(self, groq_key, openrouter_key)
        self.raid_handler = handle_raid_protection
        self.command_router = CommandRouter(self)

        self._pid_fd: int | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._shutting_down = False
        self.start_time: float = 0.0
        self.bot_id: int = 0
        self.bot_username: str = ""

        self.dp["persistence"] = self.persistence
        self.dp["pibot"] = self

        self.dp.message.middleware(PiBotMiddleware(self))

        self.dp.startup.register(self._on_startup)
        self.dp.shutdown.register(self._on_shutdown)

        self.dp.message(Command("start"))(cmd_start)
        self.dp.message()(self.handle_message)
        self.dp.chat_member()(on_chat_member)
        self.dp.callback_query(lambda c: c.data and c.data.startswith("aichange:"))(
            self.command_router.handle_ai_change
        )
        self.dp.callback_query(lambda c: c.data and c.data.startswith("writechat:"))(
            self.command_router.handle_write_chat
        )

    def schedule_task(self, coro: Any) -> None:
        """Run a background coroutine, tracked so shutdown can await it."""
        if self._shutting_down:
            return
        self.persistence.schedule_task(coro)

    def _load_personality(self) -> str:
        if PERSONALITY_PATH.exists():
            try:
                return PERSONALITY_PATH.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Не удалось прочитать personality.md: %s", e)
        return ""

    async def _on_startup(self) -> None:
        await self.persistence.init_db()
        self.dev_ids.update(self.persistence.dev_ids)
        await self.ai_service.ensure_provider()
        self.persistence.start_periodic_flush()
        bot_user = await self.bot.me()
        self.bot_id = bot_user.id
        self.bot_username = bot_user.username.lower() if bot_user.username else ""
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        for dev_id in self.dev_ids:
            try:
                await self.bot.send_message(dev_id, "✅ PiBot запущен")
            except Exception as e:
                logger.warning(
                    "[_on_startup] Не удалось уведомить разработчика %s: %s",
                    dev_id,
                    e,
                )

        self.start_time = time.time()

    async def _on_shutdown(self) -> None:
        self._shutting_down = True
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        try:
            await asyncio.wait_for(self.persistence.close(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("[shutdown] persistence close timed out")
        if self._pid_fd is not None:
            release_pid_lock(self._pid_fd)
            self._pid_fd = None

    async def _periodic_cleanup(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await self._cleanup_caches()
            except Exception as e:
                logger.warning("[cleanup] %s", e)

    def run(self) -> None:
        asyncio.run(
            self.dp.start_polling(
                self.bot,
                timeout=60,
                allowed_updates=["message", "callback_query", "chat_member"],
            )
        )

    def _get_msg_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self.msg_locks:
            self.msg_locks[chat_id] = asyncio.Lock()
        return self.msg_locks[chat_id]

    async def safe_reply(
        self, message: Message, text: str, **kwargs: Any
    ) -> Message | None:
        chunks = chunk_text(text, MAX_MESSAGE_LENGTH)
        total = len(chunks)
        sent: Message | None = None
        for index, chunk in enumerate(chunks):
            if total > 1:
                prefix = f"[{index + 1}/{total}] "
                chunk = (prefix + chunk)[:MAX_MESSAGE_LENGTH]
            reply_to = message.message_id if index == 0 else None
            try:
                if reply_to is not None:
                    sent = await message.answer(
                        chunk, reply_to_message_id=reply_to, **kwargs
                    )
                else:
                    sent = await message.answer(chunk, **kwargs)
                if sent and sent.chat.type in ("group", "supergroup"):
                    ids = self.bot_message_ids.setdefault(sent.chat.id, deque())
                    ids.append(sent.message_id)
                    while len(ids) > MAX_TRACKED_MESSAGES:
                        ids.popleft()
            except Exception as e:
                logger.warning("[safe_reply] Failed to send message: %s", e)
                return sent
        return sent

    async def handle_message(
        self, message: Message, chat_data: Any, bot_data: dict
    ) -> None:
        if not self._pre_check(message, chat_data, bot_data):
            return
        text = message.text.strip() if message.text else ""
        if not text:
            return
        if await self.command_router.handle_command(message, chat_data, bot_data, text):
            return
        user = message.from_user
        assert user is not None
        if is_user_ignored(chat_data, user.id):
            return
        lower_text = text.lower().translate(STRIP_PUNCT)
        if await self.chat_service.handle_rp(message, chat_data, lower_text):
            return
        if await self.chat_service.handle_phrase(message, chat_data, lower_text):
            return
        if not await self.ai_service.handle_ai(message, chat_data, bot_data):
            await self.ai_service.handle_random_reply(message, chat_data, bot_data)

    def _pre_check(self, message: Message, chat_data: Any, bot_data: dict) -> bool:
        if not message.text:
            return False
        user = message.from_user
        if user and user.id in bot_data.get("banned_users", []):
            return False
        if message.date:
            msg_date = message.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            msg_age = (datetime.now(timezone.utc) - msg_date).total_seconds()
            if msg_age > MAX_MESSAGE_AGE:
                if not message.text.lower().startswith(PIBOT_PREFIX):
                    return False
        return True

    async def _cleanup_caches(self) -> None:
        known = self.persistence.known_chats
        now = time.time()

        for cid in list(self.msg_locks):
            if cid not in known:
                del self.msg_locks[cid]

        for cid in list(self.bot_message_ids):
            if cid not in known:
                del self.bot_message_ids[cid]

        for chat_id in list(self.persistence._chat_data_cache.keys()):
            if chat_id not in known:
                del self.persistence._chat_data_cache[chat_id]
                self.persistence._chat_data_loaded_at.pop(chat_id, None)
                continue

            chat_data = self.persistence._chat_data_cache[chat_id]

            trigger_spam = chat_data.trigger_spam
            if trigger_spam:
                cutoff = now - TRIGGER_SPAM_WINDOW
                for uid, phrases in list(trigger_spam.items()):
                    for phrase, ts in list(phrases.items()):
                        ts[:] = [t for t in ts if t > cutoff]
                        if not ts:
                            del phrases[phrase]
                    if not phrases:
                        del trigger_spam[uid]
                if not trigger_spam:
                    chat_data.trigger_spam = {}

            spam_tracker = chat_data.spam_tracker
            if spam_tracker:
                cutoff = now - ANTISPAM_WINDOW
                for uid, ts in list(spam_tracker.items()):
                    ts[:] = [t for t in ts if t > cutoff]
                    if not ts:
                        del spam_tracker[uid]
                if not spam_tracker:
                    chat_data.spam_tracker = {}

            spam_warned = chat_data.spam_warned
            if spam_warned:
                for uid, warned_at in list(spam_warned.items()):
                    if now - warned_at > ANTISPAM_MUTE_DURATION:
                        del spam_warned[uid]
                if not spam_warned:
                    chat_data.spam_warned = {}

            if chat_data.drop_expired(now):
                self.persistence._chat_data_loaded_at[chat_id] = 0.0

        self.ai_service.conversation_history.prune()
        self.ai_service.chat_history.prune()
        await self.persistence.flush()


def load_config() -> dict[str, str]:
    token = os.getenv("TELEGRAM_TOKEN", "")

    if not token or token == "YOUR-TELEGRAM-TOKEN":
        raise ValueError(
            "❌ Токен не настроен!\n"
            "Вставьте токен в .env (TELEGRAM_TOKEN) (получить у @BotFather)"
        )

    groq_key = os.getenv("GROQ_KEY", "")
    openrouter_key = os.getenv("OPENROUTER_KEY", "")

    return {"token": token, "groq_key": groq_key, "openrouter_key": openrouter_key}


def main() -> None:
    load_dotenv(BASE / ".env")

    try:
        cfg = load_config()
    except ValueError as e:
        setup_logging(
            console_log_level=logging.DEBUG,
            file_log_level=logging.WARNING,
            file_path=BASE / "logs" / "logs.log",
        )
        logger.error(e)
        return

    token = cfg["token"]
    groq_key = cfg["groq_key"]
    openrouter_key = cfg["openrouter_key"]

    setup_logging(
        console_log_level=logging.DEBUG,
        file_log_level=logging.WARNING,
        file_path=BASE / "logs" / "logs.log",
    )

    bot = PiBot(token, groq_key, openrouter_key)
    bot._pid_fd = acquire_pid_lock()
    logger.info("PiBot started...")
    bot.run()


if __name__ == "__main__":
    main()
