import asyncio
import fcntl
import json
import logging
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, MessageEntityType, ParseMode
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatPermissions,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
    User,
)
from dotenv import load_dotenv
from groq import AsyncGroq, RateLimitError as GroqRateLimitError
from logging_settings import setup_logging
from openai import AsyncOpenAI, RateLimitError as OpenAIRateLimitError
from persistence import SQLitePersistence
from filtering import FilterManager, FILTERS
from telemetry import Telemetry

logger = logging.getLogger(__name__)

BASE = Path(__file__).parent.parent
PHRASES_PATH = BASE / "bot-data" / "phrases.json"
LINKS_PATH = BASE / "bot-data" / "links.json"
RP_COMMANDS_PATH = BASE / "bot-data" / "rp-phrases.json"
DEV_IDS_PATH = BASE / "env" / "dev-ids.json"
PERSONALITY_PATH = BASE / "bot-data" / "personality.md"
TELEMETRY_PATH = BASE / "logs" / "telemetry.json"

AI_MAX_HISTORY = 20
AI_RETRY_MAX_ATTEMPTS = 3
AI_RETRY_BASE_DELAY = 1.0
GROQ_MODEL = "llama-3.3-70b-versatile"
OPENROUTER_MODEL = "poolside/laguna-s-2.1:free" # openai/gpt-oss-20b:free

PID_FILE = BASE / "pibot.pid"

MAX_TRACKED_MESSAGES = 1000
DELETE_BATCH_SIZE = 100
MAX_MESSAGE_AGE = 120
TRIGGER_SPAM_WINDOW = 10
TRIGGER_SPAM_LIMIT = 5
TRIGGER_SPAM_MUTE = 60
ANTISPAM_WINDOW = 1.0
ANTISPAM_MSG_LIMIT = 5
ANTISPAM_MUTE_THRESHOLD = 9
ANTISPAM_MUTE_DURATION = 60
ANTISPAM_MAX_MESSAGE_AGE = 180  # 3 minutes
FILTER_MUTE_DURATION = 60
PIBOT_PREFIX = "пибот "
PIBOT_PREFIX_LEN = len(PIBOT_PREFIX)
RANDOM_REPLY_CHANCE = 0.30
MIN_RANDOM_MSG_LENGTH = 30

RANK_OWNER = 1
RANK_ADMIN_PLUS = 2
RANK_ADMIN = 3
RANK_MEMBER = 4


def pluralize_minutes(n: int) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 19:
        return "минут"
    last = n % 10
    if last == 1:
        return "минуту"
    if 2 <= last <= 4:
        return "минуты"
    return "минут"


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

STRIP_PUNCT = str.maketrans("", "", string.punctuation)


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


@dataclass
class CommandConfig:
    handler: Callable
    value: int


class RateLimiter:
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


@dataclass
class AIResponse:
    content: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class AIBackend:
    def __init__(
        self,
        name: str,
        display_name: str,
        client: Any | None,
        model: str,
        rate_limit_error: type[Exception] | None = None,
    ) -> None:
        self.name = name
        self.display_name = display_name
        self.client = client
        self.model = model
        self._rate_limit_error = rate_limit_error

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def generate(self, messages: list[dict]) -> AIResponse:
        last_error: Exception | None = None
        for attempt in range(AI_RETRY_MAX_ATTEMPTS):
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=0.7,
                    max_tokens=512,
                )
                if not response or not response.choices:
                    logger.error("[%s] Invalid API response: %s", self.name, response)
                    return AIResponse(
                        content="⚠️ Ошибка при получении ответа от ИИ",
                        model=self.model,
                    )
                content = (
                    response.choices[0].message.content or "⚠️ ИИ ничего не ответил"
                )
                usage = response.usage
                return AIResponse(
                    content=content,
                    model=response.model,
                    prompt_tokens=usage.prompt_tokens if usage else None,
                    completion_tokens=usage.completion_tokens if usage else None,
                    total_tokens=usage.total_tokens if usage else None,
                )
            except Exception as e:
                if self._rate_limit_error and isinstance(e, self._rate_limit_error):
                    raise
                last_error = e
                if attempt < AI_RETRY_MAX_ATTEMPTS - 1:
                    delay = AI_RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        "[%s] API error (attempt %d/%d): %s. Retrying in %.1fs...",
                        self.name,
                        attempt + 1,
                        AI_RETRY_MAX_ATTEMPTS,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
        logger.error(
            "[%s] API error after %d attempts: %s",
            self.name,
            AI_RETRY_MAX_ATTEMPTS,
            last_error,
        )
        raise last_error  # type: ignore[misc]


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


def get_mention(user: User) -> str:
    return f"@{user.username}" if user.username else (user.first_name or "User")


def is_user_ignored(chat_data: dict, user_id: int) -> bool:
    ignored = chat_data.get("ignored_until", {})
    expiry = ignored.get(user_id)
    if expiry is None:
        return False
    if time.time() >= expiry:
        del ignored[user_id]
        return False
    return True


def track_trigger_spam(chat_data: dict, user_id: int, phrase: str, persistence: Any = None, chat_id: int = 0) -> bool:
    now = time.time()
    trackers = chat_data.setdefault("trigger_spam", {})
    user_tracker = trackers.setdefault(user_id, {})
    timestamps = user_tracker.setdefault(phrase, [])
    cutoff = now - TRIGGER_SPAM_WINDOW
    timestamps[:] = [t for t in timestamps if t > cutoff]
    timestamps.append(now)
    if len(timestamps) > TRIGGER_SPAM_LIMIT:
        ignored = chat_data.setdefault("ignored_until", {})
        ignored[user_id] = now + TRIGGER_SPAM_MUTE
        if persistence and chat_id:
            asyncio.create_task(
                persistence.set_chat_ignored(chat_id, user_id, now + TRIGGER_SPAM_MUTE)
            )
        return True
    return False


async def get_user_rank(chat: Chat, chat_data: dict, user_id: int) -> int:
    try:
        member = await chat.get_member(user_id)
        if member.status == ChatMemberStatus.CREATOR:
            return RANK_OWNER
        if member.status == ChatMemberStatus.ADMINISTRATOR:
            return RANK_ADMIN
    except Exception:
        pass
    ranks = chat_data.setdefault("ranks", {})
    return ranks.get(user_id, RANK_MEMBER)


async def target_immune_to_mkb(
    chat: Chat, chat_data: dict, target_user_id: int
) -> bool:
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
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user

    if message.entities:
        for entity in message.entities:
            if entity.type == MessageEntityType.TEXT_MENTION:
                return entity.user
            elif entity.type == MessageEntityType.MENTION:
                start = entity.offset
                end = entity.offset + entity.length
                mention_text = message.text[start:end]
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


class PiBotMiddleware(BaseMiddleware):
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

        self.pibot._chat_transient.setdefault(chat_id, {})

        ids = chat_data.setdefault("message_ids", deque())
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
            "llm_provider": persistence._bot_config.get("llm_provider", ""),
        }

        data["chat_data"] = chat_data
        data["bot_data"] = bot_data
        data["_persistence"] = persistence

        if not user or user.is_bot:
            return await handler(event, data)

        if event.text and event.chat.type in ("group", "supergroup"):
            mention = (
                event.from_user.username
                or event.from_user.first_name
                or str(event.from_user.id)
            )
            history = self.pibot.chat_history.setdefault(chat_id, [])
            history.append({
                "username": mention,
                "text": event.text,
                "message_id": event.message_id,
            })
            if len(history) > MAX_TRACKED_MESSAGES:
                history[:] = history[-MAX_TRACKED_MESSAGES:]

        if event.chat.type not in ("group", "supergroup"):
            return await handler(event, data)

        if user.id in persistence.banned_users:
            return await handler(event, data)

        user_rank = await get_user_rank(event.chat, chat_data, user.id)
        if user_rank <= RANK_ADMIN:
            return await handler(event, data)

        if event.text:
            result = self.pibot.filter_manager.check(
                event.text, event.date, user.id, chat_id
            )
            if result.matched:
                try:
                    await event.delete()
                except Exception as e:
                    logger.warning("[Filter] не удалось удалить сообщение: %s", e)
                if result.should_mute:
                    try:
                        bot_instance: Bot = data["bot"]
                        kwargs: dict[str, Any] = {}
                        if result.mute_until is not None:
                            kwargs["until_date"] = result.mute_until
                        await bot_instance.restrict_chat_member(
                            chat_id,
                            user.id,
                            permissions=NO_PERMISSIONS,
                            **kwargs,
                        )
                    except Exception as e:
                        logger.warning("[Filter] не удалось замутить: %s", e)
                return None

        if event.date:
            msg_date = event.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            msg_age = (datetime.now(timezone.utc) - msg_date).total_seconds()
            if msg_age > ANTISPAM_MAX_MESSAGE_AGE:
                return await handler(event, data)

        if time.time() - self.pibot.start_time < 15:
            return await handler(event, data)

        now = time.time()
        spam = chat_data.setdefault("spam_tracker", {})
        ts = spam.setdefault(user.id, [])
        cutoff = now - ANTISPAM_WINDOW
        ts[:] = [t for t in ts if t > cutoff]
        ts.append(now)

        if len(ts) <= ANTISPAM_MSG_LIMIT:
            return await handler(event, data)

        if event.text:
            lower = event.text.lower().strip().translate(STRIP_PUNCT)
            if lower in self.pibot.phrases or lower.startswith(PIBOT_PREFIX + "инфа"):
                return await handler(event, data)
            if event.reply_to_message:
                if lower in self.pibot.rp_commands:
                    return await handler(event, data)

        spammer_name = (
            f"@{user.username}" if user.username else user.first_name or str(user.id)
        )

        bot_instance: Bot = data["bot"]

        if len(ts) <= ANTISPAM_MUTE_THRESHOLD:
            warned = chat_data.setdefault("spam_warned", {})
            if user.id not in warned:
                warned[user.id] = now
                await bot_instance.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {spammer_name}, пожалуйста, не флуди!",
                )
            return None

        try:
            await bot_instance.restrict_chat_member(
                chat_id,
                user.id,
                permissions=NO_PERMISSIONS,
                until_date=int(now + ANTISPAM_MUTE_DURATION),
            )
        except Exception as e:
            logger.warning("[AntiSpam] не получилось замутить: %s", e)
            return await handler(event, data)

        await bot_instance.send_message(
            chat_id=chat_id,
            text=f"✅️ Я замутил {spammer_name} за спам.",
        )
        return None


class PiBot:
    def __init__(
        self, token: str, groq_key: str = "", openrouter_key: str = ""
    ) -> None:
        self.phrases = load_phrases()
        self.links = load_links()
        self.rp_commands = load_rp_commands()
        self.dev_ids = load_dev_ids()
        self.filter_manager = FilterManager(FILTERS, mute_duration=FILTER_MUTE_DURATION)

        self.banned_users: set[int] = set()

        self.msg_locks: dict[int, asyncio.Lock] = {}
        self.bot_message_ids: dict[int, deque[int]] = {}
        self.replied_to_ids: dict[int, set[int]] = {}
        self.rate_limiter = RateLimiter(max_calls=5, period=1.0)
        self.telemetry = Telemetry(TELEMETRY_PATH)

        self.commands: dict[str, CommandConfig] = {}
        self._register_commands()

        self.persistence = SQLitePersistence(
            db_path=Path(__file__).parent / "bot_data.db"
        )

        self._chat_transient: dict[int, dict[str, Any]] = {}

        self.bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()

        self.dp["persistence"] = self.persistence
        self.dp["pibot"] = self

        self.dp.message.middleware(PiBotMiddleware(self))

        self.dp.startup.register(self._on_startup)
        self.dp.shutdown.register(self._on_shutdown)

        self.dp.message()(self.handle_message)
        self.dp.callback_query(lambda c: c.data and c.data.startswith("aichange:"))(
            self._handle_ai_change
        )
        self.dp.callback_query(lambda c: c.data and c.data.startswith("writechat:"))(
            self._handle_write_chat
        )

        self._pid_fd: int | None = None
        self.start_time: float = 0.0
        self.bot_id: int = 0
        self.bot_username: str = ""

        # AI providers
        self.providers: dict[str, AIBackend] = {}
        self.personality: str = self._load_personality()
        if not self.personality:
            logger.warning("AI: personality.md не найден, используется заглушка")
            self.personality = (
                "Ты — Пибот, 25-летняя томбой. Отвечай коротко и по делу."
            )
        self.conversation_history: dict[int, list[dict]] = {}
        self.chat_history: dict[int, list[dict]] = {}
        self.pending_writes: dict[int, str] = {}
        self.ai_limiter = RateLimiter(max_calls=2, period=60.0)
        self._init_providers(groq_key, openrouter_key)

    def _init_providers(self, groq_key: str, openrouter_key: str) -> None:
        groq_client = AsyncGroq(api_key=groq_key) if groq_key else None
        self.providers["groq"] = AIBackend(
            name="groq",
            display_name="Groq",
            client=groq_client,
            model=GROQ_MODEL,
            rate_limit_error=GroqRateLimitError,
        )

        or_client = (
            AsyncOpenAI(
                api_key=openrouter_key,
                base_url="https://openrouter.ai/api/v1",
            )
            if openrouter_key
            else None
        )
        self.providers["openrouter"] = AIBackend(
            name="openrouter",
            display_name="OpenRouter",
            client=or_client,
            model=OPENROUTER_MODEL,
            rate_limit_error=OpenAIRateLimitError,
        )

        enabled = [p for p in self.providers.values() if p.enabled]
        logger.info(
            "AI: инициализировано %d провайдеров"
            if enabled
            else "AI: ни один провайдер не настроен",
            len(enabled),
        )

    async def _ensure_ai_provider(self) -> None:
        name = self.persistence._bot_config.get("llm_provider", "")
        if name in self.providers and self.providers[name].enabled:
            return
        for p in self.providers.values():
            if p.enabled:
                await self.persistence.set_bot_config("llm_provider", p.name)
                break
        else:
            logger.warning("No enabled AI provider available")

    def _get_current_provider(self, bot_data: dict) -> AIBackend | None:
        name = bot_data.get("llm_provider", "")
        provider = self.providers.get(name)
        if provider and provider.enabled:
            return provider
        for p in self.providers.values():
            if p.enabled:
                return p
        logger.warning("No enabled AI provider found")
        return None

    def _register_commands(self) -> None:
        self.commands["сотри"] = CommandConfig(self.handle_nuke, 2)
        self.commands["кикни"] = CommandConfig(self.handle_kick, 2)
        self.commands["ликвидируй"] = CommandConfig(self.handle_ban, 1)
        self.commands["верни"] = CommandConfig(self.handle_unban, 1)
        self.commands["заблокируй"] = CommandConfig(self.handle_block, 0)
        self.commands["разблокируй"] = CommandConfig(self.handle_unblock, 0)
        self.commands["мут"] = CommandConfig(self.handle_mute, 3)
        self.commands["размут"] = CommandConfig(self.handle_unmute, 3)
        self.commands["ранг"] = CommandConfig(self.handle_rank, 1)
        self.commands["био"] = CommandConfig(self.handle_botinfo_cmd, 4)
        self.commands["обновы"] = CommandConfig(self.handle_changelog_cmd, 4)
        self.commands["команды"] = CommandConfig(self.handle_commands_cmd, 4)
        self.commands["дев команды"] = CommandConfig(self.handle_devcommands_cmd, 0)
        self.commands["ранги"] = CommandConfig(self.handle_rank_list, 4)
        self.commands["инфа"] = CommandConfig(self.handle_chance_cmd, 4)
        self.commands["все чаты"] = CommandConfig(self.handle_all_chats, 0)
        self.commands["ии"] = CommandConfig(self.handle_ai_cmd, 0)
        self.commands["очистка бд"] = CommandConfig(self.handle_clear_db, 0)
        self.commands["клонируй"] = CommandConfig(self.handle_git_clone, 2)
        self.commands["напиши"] = CommandConfig(self.handle_write_cmd, 0)

    async def _on_startup(self) -> None:
        await self.persistence.init_db()
        self.banned_users = set(self.persistence.banned_users)
        self.dev_ids.update(self.persistence.dev_ids)
        await self._ensure_ai_provider()
        self.persistence.start_periodic_flush()
        bot_user = await self.bot.me()
        self.bot_id = bot_user.id
        self.bot_username = bot_user.username.lower() if bot_user.username else ""
        asyncio.create_task(self._periodic_cleanup())
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
        await self.persistence.close()
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
                allowed_updates=["message", "callback_query"],
            )
        )

    def _get_msg_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self.msg_locks:
            self.msg_locks[chat_id] = asyncio.Lock()
        return self.msg_locks[chat_id]

    async def _read_text_file_async(self, path: Path) -> str:
        if await asyncio.to_thread(path.exists):
            return await asyncio.to_thread(path.read_text, encoding="utf-8")
        return ""

    async def track_id(self, chat_data: dict, chat_id: int, message_id: int) -> None:
        async with self._get_msg_lock(chat_id):
            ids = chat_data.setdefault("message_ids", deque())
            ids.append(message_id)
            while len(ids) > MAX_TRACKED_MESSAGES:
                ids.popleft()

    async def safe_reply(
        self, message: Message, text: str, **kwargs: Any
    ) -> Optional[Message]:
        try:
            sent = await message.answer(
                text, reply_to_message_id=message.message_id, **kwargs
            )
            if sent and sent.chat.type in ("group", "supergroup"):
                ids = self.bot_message_ids.setdefault(sent.chat.id, deque())
                ids.append(sent.message_id)
                while len(ids) > MAX_TRACKED_MESSAGES:
                    ids.popleft()
            return sent
        except Exception as e:
            logger.warning("[safe_reply] Failed to send message: %s", e)
            return None

    async def handle_message(
        self, message: Message, chat_data: dict, bot_data: dict
    ) -> None:
        if not self._pre_check(message, chat_data, bot_data):
            return
        text = message.text.strip()
        if await self._handle_command(message, chat_data, bot_data, text):
            return
        if is_user_ignored(chat_data, message.from_user.id):
            return
        lower_text = text.lower().translate(STRIP_PUNCT)
        if await self._handle_rp(message, chat_data, lower_text):
            return
        if await self._handle_phrase(message, chat_data, lower_text):
            return
        if not await self._handle_ai(message, chat_data, bot_data):
            await self._handle_random_reply(message, chat_data, bot_data)

    def _pre_check(self, message: Message, chat_data: dict, bot_data: dict) -> bool:
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

    async def _handle_command(
        self, message: Message, chat_data: dict, bot_data: dict, text: str
    ) -> bool:
        lower = text.lower()
        if not lower.startswith(PIBOT_PREFIX):
            return False
        rest = text[PIBOT_PREFIX_LEN:].strip()
        if not rest:
            return False

        words = rest.split()
        subcommand = ""
        params = rest
        for i in range(len(words), 0, -1):
            candidate = " ".join(words[:i]).lower()
            if candidate in self.commands:
                subcommand = candidate
                params = " ".join(words[i:])
                break
        if not subcommand:
            subcommand = words[0].lower()
            params = " ".join(words[1:])

        if subcommand not in self.commands:
            return False

        cmd = self.commands[subcommand]
        user_id = message.from_user.id

        if cmd.value == 0:
            if user_id not in self.dev_ids:
                await self.safe_reply(message, "⛔️ Недостаточно прав для этой команды")
                return True
        else:
            user_rank = await get_user_rank(message.chat, chat_data, user_id)
            if user_rank > cmd.value:
                await self.safe_reply(message, "⛔️ Недостаточно прав для этой команды")
                return True

        await cmd.handler(message, chat_data, bot_data, params)
        return True

    async def _handle_rp(
        self, message: Message, chat_data: dict, lower_text: str
    ) -> bool:
        if not (message.reply_to_message and message.reply_to_message.from_user):
            return False
        if lower_text not in self.rp_commands:
            return False

        if track_trigger_spam(chat_data, message.from_user.id, lower_text, self.persistence, message.chat.id):
            await self.safe_reply(message, "Ой всё", disable_notification=True)
            return True
        if not await self.rate_limiter.acquire():
            return True

        user1 = get_mention(message.from_user)
        user2 = get_mention(message.reply_to_message.from_user)
        response = (
            self.rp_commands[lower_text]
            .replace("{mention1}", user1)
            .replace("{mention2}", user2)
        )
        await self.safe_reply(message, response, disable_notification=True)
        return True

    async def _handle_phrase(
        self, message: Message, chat_data: dict, lower_text: str
    ) -> bool:
        if lower_text not in self.phrases:
            return False

        if track_trigger_spam(chat_data, message.from_user.id, lower_text, self.persistence, message.chat.id):
            await self.safe_reply(message, "Ой всё", disable_notification=True)
            return True
        if not await self.rate_limiter.acquire():
            return True

        response = self.phrases[lower_text]
        mention = get_mention(message.from_user)

        response = response.replace("{mention}", mention)
        await self.safe_reply(message, response, disable_notification=True)
        return True

    def _load_personality(self) -> str:
        if PERSONALITY_PATH.exists():
            try:
                return PERSONALITY_PATH.read_text(encoding="utf-8")
            except Exception as e:
                logger.warning("Не удалось прочитать personality.md: %s", e)
        return ""

    async def _is_bot_mentioned(self, message: Message) -> bool:
        if not message.entities:
            return False
        for entity in message.entities:
            if entity.type == MessageEntityType.MENTION:
                mention = message.text[entity.offset : entity.offset + entity.length]
                if mention[1:].lower() == self.bot_username:
                    return True
            elif entity.type == MessageEntityType.TEXT_MENTION:
                if entity.user and entity.user.id == self.bot_id:
                    return True
        return False

    def _strip_mention(self, message: Message) -> str:
        text = message.text
        if not message.entities:
            return text.strip()
        for entity in sorted(message.entities, key=lambda e: e.offset, reverse=True):
            if entity.type == MessageEntityType.MENTION:
                mention = text[entity.offset : entity.offset + entity.length]
                if mention[1:].lower() == self.bot_username:
                    text = text[: entity.offset] + text[entity.offset + entity.length :]
            elif entity.type == MessageEntityType.TEXT_MENTION:
                if entity.user and entity.user.id == self.bot_id:
                    text = text[: entity.offset] + text[entity.offset + entity.length :]
        return text.strip()

    def _build_ai_messages(self, message: Message, query: str) -> list[dict]:
        messages = [{"role": "system", "content": self.personality}]

        if message.chat.type == "private":
            history = self.conversation_history.get(message.chat.id, [])
            messages.extend(history)
        else:
            history = self.chat_history.get(message.chat.id, [])
            context_lines = [
                f"@{entry['username']} said: {entry['text']}"
                for entry in history[-AI_MAX_HISTORY:]
            ]
            if context_lines:
                messages.append(
                    {
                        "role": "system",
                        "content": "Recent chat history:\n" + "\n".join(context_lines),
                    }
                )

        messages.append({"role": "user", "content": query})
        return messages

    async def _handle_ai(
        self, message: Message, chat_data: dict, bot_data: dict
    ) -> bool:
        provider = self._get_current_provider(bot_data)
        if not provider or not provider.enabled:
            logger.warning("No available AI provider for message handling")
            return False

        if message.chat.type in ("group", "supergroup"):
            if not await self._is_bot_mentioned(message):
                return False

        if not await self.ai_limiter.acquire():
            await self.safe_reply(
                message,
                "⚠️ Лимит апиииииии. Жди минуту ",
            )
            return True

        query = (
            self._strip_mention(message)
            if message.chat.type in ("group", "supergroup")
            else message.text.strip()
        )
        if not query:
            return False

        ai_messages = self._build_ai_messages(message, query)

        start = time.monotonic()
        for attempt in range(AI_RETRY_MAX_ATTEMPTS):
            try:
                ai_response = await provider.generate(ai_messages)
                break
            except (GroqRateLimitError, OpenAIRateLimitError):
                if attempt < AI_RETRY_MAX_ATTEMPTS - 1:
                    delay = AI_RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        "[%s] 429 Rate Limit (attempt %d/%d). Retrying in %.1fs...",
                        provider.name,
                        attempt + 1,
                        AI_RETRY_MAX_ATTEMPTS,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                duration = round((time.monotonic() - start) * 1000)
                logger.warning(
                    "[%s] 429 Rate Limit after %d attempts",
                    provider.name,
                    AI_RETRY_MAX_ATTEMPTS,
                )
                await self.telemetry.record(
                    {
                        "user_id": message.from_user.id,
                        "username": message.from_user.username,
                        "chat_id": message.chat.id,
                        "chat_type": message.chat.type,
                        "provider": provider.name,
                        "model": provider.model,
                        "success": False,
                        "duration_ms": duration,
                        "error": "RateLimitError",
                    }
                )
                await self.safe_reply(
                    message,
                    "⚠️ Ты достиг лимита апи, задрот",
                )
                return True
            except Exception as e:
                duration = round((time.monotonic() - start) * 1000)
                logger.error("[%s] API error: %s", provider.name, e, exc_info=True)
                await self.telemetry.record(
                    {
                        "user_id": message.from_user.id,
                        "username": message.from_user.username,
                        "chat_id": message.chat.id,
                        "chat_type": message.chat.type,
                        "provider": provider.name,
                        "model": provider.model,
                        "success": False,
                        "duration_ms": duration,
                        "error": str(e),
                    }
                )
                await self.safe_reply(message, "⚠️ Ошибка при обращении к ИИ")
                return True
        else:
            return True

        duration = round((time.monotonic() - start) * 1000)
        answer = ai_response.content

        await self.telemetry.record(
            {
                "user_id": message.from_user.id,
                "username": message.from_user.username,
                "chat_id": message.chat.id,
                "chat_type": message.chat.type,
                "provider": provider.name,
                "model": ai_response.model,
                "success": True,
                "duration_ms": duration,
                "prompt_tokens": ai_response.prompt_tokens,
                "completion_tokens": ai_response.completion_tokens,
                "total_tokens": ai_response.total_tokens,
            }
        )

        if message.chat.type == "private":
            history = self.conversation_history.setdefault(message.chat.id, [])
            history.append({"role": "user", "content": query})
            history.append({"role": "assistant", "content": answer})

        await self.safe_reply(message, answer, disable_notification=True)
        return True

    async def _handle_random_reply(
        self, message: Message, chat_data: dict, bot_data: dict
    ) -> None:
        if message.chat.type not in ("group", "supergroup"):
            return

        if random.random() >= RANDOM_REPLY_CHANCE:
            return

        history = self.chat_history.get(message.chat.id, [])
        replied = self.replied_to_ids.setdefault(message.chat.id, set())
        candidates = [
            entry for entry in history[-AI_MAX_HISTORY:]
            if len(entry["text"]) > MIN_RANDOM_MSG_LENGTH
            and entry["message_id"] != message.message_id
            and entry["message_id"] not in replied
        ]
        if not candidates:
            return

        provider = self._get_current_provider(bot_data)
        if not provider or not provider.enabled:
            return

        if not await self.ai_limiter.acquire():
            return

        chosen = random.choice(candidates)

        context_lines = [
            f"@{entry['username']} said: {entry['text']}"
            for entry in history[-AI_MAX_HISTORY:]
        ]
        ai_messages: list[dict[str, str]] = [
            {"role": "system", "content": self.personality},
        ]
        if context_lines:
            ai_messages.append({
                "role": "system",
                "content": "Recent chat history:\n" + "\n".join(context_lines),
            })
        ai_messages.append({
            "role": "user",
            "content": (
                f"Someone wrote in chat:\n"
                f"@{chosen['username']}: {chosen['text']}\n\n"
                f"Write a short relevant reply to this message as if you're a "
                f"chat participant. Keep it concise and natural."
            ),
        })

        start = time.monotonic()
        for attempt in range(AI_RETRY_MAX_ATTEMPTS):
            try:
                ai_response = await provider.generate(ai_messages)
                break
            except (GroqRateLimitError, OpenAIRateLimitError):
                if attempt < AI_RETRY_MAX_ATTEMPTS - 1:
                    delay = AI_RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        "[%s] 429 Rate Limit random reply (attempt %d/%d). Retrying in %.1fs...",
                        provider.name,
                        attempt + 1,
                        AI_RETRY_MAX_ATTEMPTS,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "[%s] 429 Rate Limit random reply after %d attempts",
                    provider.name,
                    AI_RETRY_MAX_ATTEMPTS,
                )
                return
            except Exception as e:
                logger.error(
                    "[%s] API error random reply: %s", provider.name, e, exc_info=True
                )
                return
        else:
            return

        duration = round((time.monotonic() - start) * 1000)
        answer = ai_response.content

        await self.telemetry.record(
            {
                "user_id": message.from_user.id,
                "username": message.from_user.username,
                "chat_id": message.chat.id,
                "chat_type": message.chat.type,
                "provider": provider.name,
                "model": ai_response.model,
                "success": True,
                "duration_ms": duration,
                "prompt_tokens": ai_response.prompt_tokens,
                "completion_tokens": ai_response.completion_tokens,
                "total_tokens": ai_response.total_tokens,
                "random_reply": True,
            }
        )

        try:
            await self.bot.send_message(
                chat_id=message.chat.id,
                text=answer,
                reply_to_message_id=chosen["message_id"],
                disable_notification=True,
            )
            replied.add(chosen["message_id"])
        except Exception as e:
            logger.warning("[random_reply] Failed to send message: %s", e)

    async def handle_nuke(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        if not params:
            await self.safe_reply(
                message,
                "Использование: пибот сотри n, где n - целое положительное число",
            )
            return

        try:
            n = int(params)
            if n <= 0:
                raise ValueError
        except ValueError:
            await self.safe_reply(
                message,
                "Использование: пибот сотри n, где n - целое положительное число",
            )
            return

        chat_id = message.chat.id
        async with self._get_msg_lock(chat_id):
            user_ids = chat_data.setdefault("message_ids", deque())
            bot_ids = self.bot_message_ids.setdefault(chat_id, deque())

            all_ids = set(user_ids) | set(bot_ids)
            if not all_ids:
                await self.safe_reply(message, "⚠️ Не найдено сообщений")
                return

            sorted_ids = sorted(all_ids)
            count = min(n + 1, len(sorted_ids))
            ids_to_delete = sorted_ids[-count:]

            for mid in ids_to_delete:
                if mid in user_ids:
                    user_ids.remove(mid)
                if mid in bot_ids:
                    bot_ids.remove(mid)

        if not ids_to_delete:
            await self.safe_reply(message, "⚠️ Нет сообщений для удаления")
            return

        total = len(ids_to_delete)
        deleted = 0
        for i in range(0, total, DELETE_BATCH_SIZE):
            batch = ids_to_delete[i : i + DELETE_BATCH_SIZE]
            try:
                await message.bot.delete_messages(chat_id=chat_id, message_ids=batch)
                deleted += len(batch)
            except Exception as e:
                logger.warning(
                    "[Nuke] не удалось удалить %d сообщений: %s",
                    len(batch),
                    e,
                )

        if deleted == 0:
            await self.safe_reply(message, "⚠️ Не удалось удалить сообщения")

    async def handle_kick(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        target = await resolve_user(message, bot_data, message.chat, params)
        if not target:
            await self.safe_reply(
                message,
                "⚠️ Кого выкинуть? Ответь на сообщение или укажи @username",
            )
            return

        if await target_immune_to_mkb(message.chat, chat_data, target.id):
            await self.safe_reply(message, "⛔️ Этого пользователя нельзя выкинуть")
            return

        try:
            await message.bot.ban_chat_member(message.chat.id, target.id)
            await message.bot.unban_chat_member(message.chat.id, target.id)
            await self.safe_reply(message, f"✅️ {get_mention(target)} выкинут за борт")
        except Exception as e:
            await self.safe_reply(message, f"⚠️ Ошибка кика: {e}")

    async def handle_ban(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        target = await resolve_user(message, bot_data, message.chat, params)
        if not target:
            await self.safe_reply(
                message,
                "⚠️ Цель не найдена. Ответь на сообщение или укажи @username",
            )
            return

        if await target_immune_to_mkb(message.chat, chat_data, target.id):
            await self.safe_reply(message, "⛔️ Этого пользователя нельзя ликвидировать")
            return

        try:
            await message.bot.ban_chat_member(
                message.chat.id, target.id, revoke_messages=True
            )
            await self.persistence.add_global_ban(target.id)
            self.banned_users.add(target.id)
            await self.safe_reply(message, f"✅️ {get_mention(target)} был ликвидирован")
        except Exception as e:
            await self.safe_reply(message, f"⚠️ Ошибка ликвидации: {e}")

    async def handle_unban(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        target = await resolve_user(message, bot_data, message.chat, params)
        if not target:
            await self.safe_reply(
                message,
                "⚠️ Кого воскресить? Ответь на сообщение или укажи @username",
            )
            return

        try:
            await message.bot.unban_chat_member(
                message.chat.id, target.id, only_if_banned=True
            )
            await self.persistence.remove_global_ban(target.id)
            self.banned_users.discard(target.id)
            await self.safe_reply(message, f"✅️ {get_mention(target)} воскрес")
        except Exception as e:
            await self.safe_reply(message, f"⚠️ Ошибка воскрешения: {e}")

    async def handle_block(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        if not params:
            await self.safe_reply(
                message,
                "Использование: пибот заблокируй <id> или @username",
            )
            return

        target = await resolve_user(message, bot_data, message.chat, params)
        if target:
            target_id = target.id
        else:
            try:
                target_id = int(params.strip())
            except ValueError:
                await self.safe_reply(message, "⚠️ Укажи числовой ID или @username")
                return

        await self.persistence.add_global_ban(target_id)
        self.banned_users.add(target_id)
        await self.safe_reply(message, f"✅️ Пользователь {target_id} заблокирован")

    async def handle_unblock(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        if not params:
            await self.safe_reply(
                message,
                "Использование: пибот разблокируй <id> или @username",
            )
            return

        target = await resolve_user(message, bot_data, message.chat, params)
        if target:
            target_id = target.id
        else:
            try:
                target_id = int(params.strip())
            except ValueError:
                await self.safe_reply(message, "⚠️ Укажи числовой ID или @username")
                return

        if target_id not in self.banned_users:
            await self.safe_reply(
                message, f"⚠️ Пользователь {target_id} не в чёрном списке"
            )
            return

        self.banned_users.discard(target_id)
        await self.persistence.remove_global_ban(target_id)
        await self.safe_reply(message, f"✅️ Пользователь {target_id} разблокирован")

    async def handle_mute(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        duration_minutes = None
        user_params = params

        if params:
            parts = params.rsplit(maxsplit=1)
            if len(parts) == 2:
                try:
                    duration_minutes = float(parts[1])
                    if duration_minutes >= 0.5:
                        user_params = parts[0]
                    else:
                        duration_minutes = None
                except ValueError:
                    pass
            elif message.reply_to_message:
                try:
                    duration_minutes = float(parts[0])
                    if duration_minutes >= 0.5:
                        user_params = ""
                except ValueError:
                    pass

        target = await resolve_user(message, bot_data, message.chat, user_params)
        if not target:
            await self.safe_reply(
                message,
                "⚠️ Кого мутить? Ответь на сообщение или укажи @username",
            )
            return

        if await target_immune_to_mkb(message.chat, chat_data, target.id):
            await self.safe_reply(message, "⛔️ Этого пользователя нельзя замутить")
            return

        try:
            if duration_minutes is not None:
                until_date = int(time.time()) + int(duration_minutes * 60)
                await message.bot.restrict_chat_member(
                    message.chat.id,
                    target.id,
                    permissions=NO_PERMISSIONS,
                    until_date=until_date,
                )
                # end_str = datetime.fromtimestamp(until_date).strftime("%H:%M")
                await self.safe_reply(
                    message,
                    f"✅️ {get_mention(target)} рот прикрой на {duration_minutes} {pluralize_minutes(int(duration_minutes))}",
                )
            else:
                await message.bot.restrict_chat_member(
                    message.chat.id,
                    target.id,
                    permissions=NO_PERMISSIONS,
                )
                await self.safe_reply(
                    message, f"✅️ {get_mention(target)} не глаголь тут"
                )
        except Exception as e:
            await self.safe_reply(message, f"⚠️ Ошибка мута: {e}")

    async def handle_unmute(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        target = await resolve_user(message, bot_data, message.chat, params)
        if not target:
            await self.safe_reply(
                message,
                "⚠️ Кого размутить? Ответь на сообщение или укажи @username",
            )
            return

        try:
            await message.bot.restrict_chat_member(
                message.chat.id,
                target.id,
                permissions=ALL_PERMISSIONS,
            )
            await self.safe_reply(message, f"✅️ {get_mention(target)} больше не буянь")
        except Exception as e:
            await self.safe_reply(message, f"⚠️ Ошибка размута: {e}")

    async def handle_rank(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        parts = params.split(maxsplit=2)
        if len(parts) < 1:
            await self.safe_reply(
                message,
                "Использование: пибот ранг n для @user (n = 2, 3, 4)",
            )
            return

        try:
            new_rank = int(parts[0])
        except ValueError:
            await self.safe_reply(
                message,
                "Использование: пибот ранг n для @user (n = 2, 3, 4)",
            )
            return

        if new_rank not in (RANK_ADMIN_PLUS, RANK_ADMIN, RANK_MEMBER):
            await self.safe_reply(message, "Ранг может быть только 2, 3 или 4")
            return

        target_str = ""
        if len(parts) >= 3 and parts[1] == "для":
            target_str = parts[2]
        elif len(parts) == 2:
            target_str = parts[1]

        target = await resolve_user(message, bot_data, message.chat, target_str)
        if not target:
            await self.safe_reply(
                message,
                "⚠️ Кому изменить ранг? Ответь на сообщение или укажи @username",
            )
            return

        bot_user = await self.bot.me()
        if target.id == bot_user.id:
            await self.safe_reply(message, "⛔️ Нельзя изменить ранг бота")
            return

        target_rank = await get_user_rank(message.chat, chat_data, target.id)
        if target_rank == RANK_OWNER:
            await self.safe_reply(message, "⛔️ Нельзя изменить ранг владельца")
            return

        try:
            target_member = await message.chat.get_member(target.id)
            is_tg_admin = target_member.status == ChatMemberStatus.ADMINISTRATOR
        except Exception:
            is_tg_admin = False

        if is_tg_admin and new_rank == RANK_MEMBER:
            await self.safe_reply(
                message,
                "⛔️ Нельзя выдать ранг 4 администратору. Понизьте его через Telegram.",
            )
            return

        if not is_tg_admin and new_rank in (RANK_ADMIN_PLUS, RANK_ADMIN):
            await self.safe_reply(
                message,
                "⛔️ Нельзя выдать ранг 2 или 3 обычному участнику. "
                "Сначала выдайте админку через Telegram.",
            )
            return

        ranks = chat_data.setdefault("ranks", {})
        ranks[target.id] = new_rank
        await self.persistence.set_chat_rank(message.chat.id, target.id, new_rank)

        rank_names = {
            RANK_ADMIN_PLUS: "Админ+",
            RANK_ADMIN: "Админ",
            RANK_MEMBER: "Участник",
        }
        await self.safe_reply(
            message,
            f"✅️ Ранг {get_mention(target)} изменён на {rank_names[new_rank]}",
        )

    async def handle_botinfo_cmd(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        link = self.links.get("био")
        if link:
            await self.safe_reply(message, link)
        else:
            await self.safe_reply(message, "⚠️ Ссылка не настроена")

    async def handle_changelog_cmd(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        link = self.links.get("обновы")
        if link:
            await self.safe_reply(message, link)
        else:
            await self.safe_reply(message, "⚠️ Ссылка не настроена")

    async def handle_commands_cmd(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        link = self.links.get("команды")
        if link:
            await self.safe_reply(message, link)
        else:
            await self.safe_reply(message, "⚠️ Ссылка не настроена")

    async def handle_devcommands_cmd(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        link = self.links.get("дев команды")
        if link:
            await self.safe_reply(message, link)
        else:
            await self.safe_reply(message, "⚠️ Ссылка не настроена")

    async def handle_rank_list(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        ranks = chat_data.setdefault("ranks", {})
        rank_names = {RANK_ADMIN_PLUS: "Admin+", RANK_ADMIN: "Admin"}
        lines = []

        for user_id, rank in ranks.items():
            if rank not in (RANK_ADMIN_PLUS, RANK_ADMIN):
                continue
            try:
                member = await message.chat.get_member(user_id)
                display = get_mention(member.user)
            except Exception:
                display = str(user_id)
            lines.append(f"{display} имеет ранг {rank} — {rank_names[rank]}")

        if not lines:
            await self.safe_reply(message, "Нет пользователей с особыми рангами")
            return

        await self.safe_reply(message, "\n".join(lines))

    async def handle_clear_db(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        try:
            await self.persistence.clear_all()
            self.banned_users.clear()
            self._chat_transient.clear()

            await self.safe_reply(message, "✅ База данных очищена")
        except Exception as e:
            logger.error("[clear_db] %s", e, exc_info=True)
            await self.safe_reply(message, f"⚠️ Ошибка очистки базы данных: {e}")

    async def handle_git_clone(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        url = params.strip()
        if not url:
            await self.safe_reply(
                message,
                "Использование: пибот клонируй <url>",
            )
            return

        if not url.startswith(("http://", "https://", "git@", "ssh://")):
            await self.safe_reply(
                message,
                "⚠️ Некорректный URL. Укажи ссылку вида https://github.com/...",
            )
            return

        status_msg = await self.safe_reply(message, "Ща, жди")

        tmp_dir = None
        zip_path = None
        try:
            tmp_dir = tempfile.mkdtemp(prefix="pibot_clone_")

            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                url,
                tmp_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_text = stderr.decode().strip() if stderr else "неизвестная ошибка"
                await self.safe_reply(
                    message, f"⚠️ Ошибка клонирования:\n{error_text[:500]}"
                )
                return

            repo_name = url.rstrip("/").split("/")[-1].removesuffix(".git") or "repo"
            zip_path = f"/tmp/{repo_name}.zip"

            try:
                await status_msg.edit_text("Ещё чуть-чуть жди")
            except Exception:
                pass

            shutil.make_archive(
                zip_path.removesuffix(".zip"),
                "zip",
                tmp_dir,
            )

            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            if size_mb > 40:
                await self.safe_reply(
                    message,
                    f"⚠️ Архив слишком большой ({size_mb:.1f} MB). Лимит 40 MB.",
                )
                return

            await message.answer_document(
                FSInputFile(zip_path),
                caption=f"{url}",
            )

            if status_msg:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

        except Exception as e:
            logger.error("[git_clone] %s", e, exc_info=True)
            await self.safe_reply(message, f"⚠️ Ошибка: {e}")
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            if zip_path and os.path.exists(zip_path):
                os.remove(zip_path)

    async def handle_chance_cmd(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        n = random.randint(0, 100)
        await self.safe_reply(
            message,
            f"Я думаю, что вероятность {n}%",
            disable_notification=True,
        )

    async def handle_all_chats(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        known = self.persistence.known_chats
        if not known:
            await self.safe_reply(message, "Нет известных чатов")
            return

        lines: list[str] = []
        for cid in sorted(known)[:50]:
            try:
                chat = await message.bot.get_chat(cid)
                title = chat.title or chat.username or chat.first_name or str(cid)
                chat_type = {
                    "private": "💬",
                    "group": "👥",
                    "supergroup": "📢",
                    "channel": "📣",
                }.get(chat.type, "❓")
                lines.append(f"{chat_type} {title} (id: {cid})")
            except Exception as e:
                lines.append(f"❌ {cid} — {e}")

        reply = "\n".join(lines)
        if len(known) > 50:
            reply += f"\n\n... и ещё {len(known) - 50} чатов"
        await self.safe_reply(message, f"📋 Все чаты ({len(known)}):\n\n{reply}")

    async def handle_ai_cmd(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        current = bot_data.get("llm_provider", "")
        buttons = []
        for p in self.providers.values():
            if p.enabled:
                label = f"{'✅ ' if p.name == current else ''}{p.display_name}"
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=label,
                            callback_data=f"aichange:{message.from_user.id}:{p.name}",
                        )
                    ]
                )

        if not buttons:
            await self.safe_reply(message, "⚠️ Нет доступных AI провайдеров")
            return

        current_name = (
            self.providers[current].display_name if current in self.providers else "—"
        )
        await self.safe_reply(
            message,
            f"🎛 Текущий AI провайдер: {current_name}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    async def _handle_ai_change(self, callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 3:
            return
        _, user_id_str, provider_name = parts
        try:
            caller_id = int(user_id_str)
        except ValueError:
            return
        if callback.from_user.id != caller_id:
            await callback.answer("⛔️ Это не твоя кнопка", show_alert=True)
            return
        if (
            provider_name not in self.providers
            or not self.providers[provider_name].enabled
        ):
            await callback.answer("⚠️ Провайдер недоступен", show_alert=True)
            return

        await self.persistence.set_bot_config("llm_provider", provider_name)

        await callback.message.edit_text(
            f"✅ AI провайдер переключён на {self.providers[provider_name].display_name}"
        )
        await callback.answer()

    async def handle_write_cmd(
        self,
        message: Message,
        chat_data: dict,
        bot_data: dict,
        params: str,
    ) -> None:
        if not params:
            await self.safe_reply(
                message, "Использование: пибот напиши <текст>"
            )
            return

        known = self.persistence.known_chats
        if not known:
            await self.safe_reply(message, "Нет известных чатов")
            return

        self.pending_writes[message.from_user.id] = params

        buttons: list[list[InlineKeyboardButton]] = []
        for cid in sorted(known)[:50]:
            try:
                chat = await message.bot.get_chat(cid)
                title = chat.title or chat.username or chat.first_name or str(cid)
            except Exception:
                title = str(cid)
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=title,
                        callback_data=f"writechat:{message.from_user.id}:{cid}",
                    )
                ]
            )

        await self.safe_reply(
            message,
            "📨 В какой чат отправить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    async def _handle_write_chat(self, callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 3:
            return
        _, user_id_str, chat_id_str = parts
        try:
            caller_id = int(user_id_str)
            target_chat_id = int(chat_id_str)
        except ValueError:
            return
        if callback.from_user.id != caller_id:
            await callback.answer("⛔️ Это не твоя кнопка", show_alert=True)
            return

        text = self.pending_writes.pop(caller_id, None)
        if text is None:
            await callback.answer("⚠️ Текст устарел, попробуй снова", show_alert=True)
            return

        try:
            await callback.bot.send_message(target_chat_id, text)
            await callback.message.edit_text(
                f"✅ Отправлено в чат {target_chat_id}"
            )
            await callback.answer()
        except Exception as e:
            await callback.answer(f"❌ Не удалось отправить: {e}", show_alert=True)

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
                continue

            chat_data = self.persistence._chat_data_cache[chat_id]

            trigger_spam = chat_data.get("trigger_spam")
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
                    del chat_data["trigger_spam"]

            spam_tracker = chat_data.get("spam_tracker")
            if spam_tracker:
                cutoff = now - ANTISPAM_WINDOW
                for uid, ts in list(spam_tracker.items()):
                    ts[:] = [t for t in ts if t > cutoff]
                    if not ts:
                        del spam_tracker[uid]
                if not spam_tracker:
                    del chat_data["spam_tracker"]

            spam_warned = chat_data.get("spam_warned")
            if spam_warned:
                for uid, warned_at in list(spam_warned.items()):
                    if now - warned_at > ANTISPAM_MUTE_DURATION:
                        del spam_warned[uid]
                if not spam_warned:
                    del chat_data["spam_warned"]

            ignored = chat_data.get("ignored_until")
            if ignored:
                for uid, expiry in list(ignored.items()):
                    if now >= expiry:
                        del ignored[uid]
                if not ignored:
                    del chat_data["ignored_until"]

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
