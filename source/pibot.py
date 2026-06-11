import asyncio
import json
import logging
import os
import random
import string
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import load_dotenv
from openai import AsyncOpenAI, RateLimitError, APITimeoutError, APIConnectionError
from persistence import SQLitePersistence
from telegram import (
    Chat,
    ChatPermissions,
    Message,
    MessageEntity,
    Update,
    User,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackContext,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

BASE = Path(__file__).parent.parent
PHRASES_PATH = BASE / "bot-data" / "phrases.json"
BOTINFO_PATH = BASE / "bot-data" / "botinfo.md"
CHANGELOG_PATH = BASE / "bot-data" / "changelog.md"
COMMANDLIST_PATH = BASE / "info" / "command-list.md"
RP_COMMANDS_PATH = BASE / "bot-data" / "rp-phrases.json"
DEV_IDS_PATH = BASE / "env" / "dev-ids.json"

MAX_TRACKED_MESSAGES = 1000
DELETE_BATCH_SIZE = 100
MAX_MESSAGE_AGE = 120
TRIGGER_SPAM_WINDOW = 60
TRIGGER_SPAM_LIMIT = 5
TRIGGER_SPAM_MUTE = 120
LLM_HISTORY_LIMIT = 20
CHAT_CONTEXT_LIMIT = 10
ANTISPAM_WINDOW = 1.0
ANTISPAM_MSG_LIMIT = 5
ANTISPAM_MUTE_THRESHOLD = 9
ANTISPAM_MUTE_DURATION = 60
PIBOT_PREFIX = "пибот "
PIBOT_PREFIX_LEN = len(PIBOT_PREFIX)

CHANCE_TRIGGER = "пибот инфа"
PERSONALITY_PATH = BASE / "bot-data" / "personality.md"

RANK_OWNER = 1
RANK_ADMIN_PLUS = 2
RANK_ADMIN = 3
RANK_MEMBER = 4

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

SPECIAL_RESPONSES = {
    "__botinfo__": (BOTINFO_PATH, "⚠️ Инфа потерялась, проверь путь к моему описанию"),
    "__changelog__": (CHANGELOG_PATH, "⚠️ Инфа потерялась, проверь путь к моим обновам"),
    "__commandlist__": (
        COMMANDLIST_PATH,
        "⚠️ Инфа потерялась, проверь путь к списку команд",
    ),
}

GULAG_PREFIXES = ("в гулаг ", "вгулаг ")

STRIP_PUNCT = str.maketrans("", "", string.punctuation)


@dataclass
class CommandConfig:
    handler: Callable
    value: int
    dev_only: bool = False


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


def load_phrases() -> dict[str, str]:
    if PHRASES_PATH.exists():
        with open(PHRASES_PATH, encoding="utf-8") as f:
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
    timestamps.append(now)
    if len(timestamps) > TRIGGER_SPAM_LIMIT:
        ignored = context.chat_data.setdefault("ignored_until", {})
        ignored[user_id] = now + TRIGGER_SPAM_MUTE
        return True
    return False


async def get_user_rank(update: Update, context: CallbackContext, user_id: int) -> int:
    try:
        member = await update.effective_chat.get_member(user_id)
        if member.status == ChatMemberStatus.OWNER:
            return RANK_OWNER
        if member.status == ChatMemberStatus.ADMINISTRATOR:
            return RANK_ADMIN
    except Exception:
        pass

    ranks = context.chat_data.setdefault("ranks", {})
    return ranks.get(user_id, RANK_MEMBER)


async def target_immune_to_mkb(
    update: Update, context: CallbackContext, target_user_id: int
) -> bool:
    rank = await get_user_rank(update, context, target_user_id)
    return rank <= RANK_ADMIN


async def _resolve_by_username(
    username: str, context: CallbackContext, chat: Chat
) -> Optional[User]:
    username_map = context.bot_data.get("username_map", {})
    if username in username_map:
        user_id = username_map[username]
        try:
            member = await chat.get_member(user_id)
            return member.user
        except Exception:
            pass
    return None


async def resolve_user(
    update: Update, context: CallbackContext, params: str
) -> Optional[User]:
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user

    chat = update.effective_chat
    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == MessageEntity.TEXT_MENTION:
                return entity.user
            elif entity.type == MessageEntity.MENTION:
                start = entity.offset
                end = entity.offset + entity.length
                mention_text = update.message.text[start:end]
                username = mention_text[1:].lower()
                result = await _resolve_by_username(username, context, chat)
                if result:
                    return result

    if params:
        username = params.strip().lstrip("@").lower()
        result = await _resolve_by_username(username, context, chat)
        if result:
            return result

    return None


def _strip_gulag_prefix(text: str) -> Optional[str]:
    for prefix in GULAG_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return None


class PiBot:
    def __init__(self, token: str) -> None:
        self.phrases = load_phrases()
        self.rp_commands = load_rp_commands()
        self.dev_ids = load_dev_ids()

        self.banned_users: set[int] = set()

        self.llm_client: Optional[AsyncOpenAI] = None
        self.personality_prompt = ""
        self._init_llm()

        self.msg_locks: dict[int, asyncio.Lock] = {}
        self.llm_rate_limiters: dict[int, RateLimiter] = {}
        self.rate_limiter = RateLimiter(max_calls=5, period=1.0)

        self.commands: dict[str, CommandConfig] = {}
        self._register_commands()

        persistence: SQLitePersistence = SQLitePersistence(
            db_path=Path(__file__).parent / "bot_data.db"
        )
        self.app = (
            Application.builder()
            .token(token)
            .persistence(persistence)
            .post_init(self.post_init)
            .build()
        )
        self.app.add_handler(
            MessageHandler(filters.ALL, self.track_all_messages), group=-1
        )
        self.app.add_handler(MessageHandler(filters.ALL, self.handle_message))

    def _init_llm(self) -> None:
        if PERSONALITY_PATH.exists():
            self.personality_prompt = PERSONALITY_PATH.read_text(
                encoding="utf-8"
            ).strip()
        else:
            logger.warning("personality.md не найден. ИИ выключен")
        try:
            groq_key = os.getenv("GROQ_KEY", "")
            if groq_key and groq_key != "YOUR-GROQ-API-KEY-HERE":
                self.llm_client = AsyncOpenAI(
                    api_key=groq_key, base_url="https://api.groq.com/openai/v1"
                )
        except Exception as e:
            logger.error("[Init] Groq client error: %s", e, exc_info=True)

    def _register_commands(self) -> None:
        self.commands["сотри"] = CommandConfig(handler=self.handle_nuke, value=2)
        self.commands["кикни"] = CommandConfig(handler=self.handle_kick, value=2)
        self.commands["кинь"] = CommandConfig(handler=self.handle_ban, value=1)
        self.commands["выкинь"] = CommandConfig(handler=self.handle_ban, value=1)
        self.commands["верни"] = CommandConfig(handler=self.handle_unban, value=1)
        self.commands["заблокируй"] = CommandConfig(
            handler=self.handle_block, value=0, dev_only=True
        )
        self.commands["мут"] = CommandConfig(handler=self.handle_mute, value=3)
        self.commands["размут"] = CommandConfig(handler=self.handle_unmute, value=3)
        self.commands["ранг"] = CommandConfig(handler=self.handle_rank, value=1)
        self.commands["био"] = CommandConfig(handler=self.handle_botinfo_cmd, value=4)
        self.commands["обновы"] = CommandConfig(
            handler=self.handle_changelog_cmd, value=4
        )
        self.commands["команды"] = CommandConfig(
            handler=self.handle_commands_cmd, value=4
        )
        self.commands["ранги"] = CommandConfig(handler=self.handle_rank_list, value=4)

    async def post_init(self, app: Application) -> None:
        self.banned_users = set(app.bot_data.get("banned_users", []))
        for chat_data in app.chat_data.values():
            chat_data.pop("llm_history", None)
        if app.job_queue:
            app.job_queue.run_repeating(self._cleanup_caches, interval=3600, first=3600)
        for dev_id in self.dev_ids:
            try:
                await app.bot.send_message(dev_id, "✅ PiBot запущен")
            except Exception as e:
                logger.warning("[post_init] Не удалось уведомить разработчика %s: %s", dev_id, e)

    def run(self) -> None:
        asyncio.set_event_loop(asyncio.new_event_loop())
        self.app.run_polling(drop_pending_updates=True)

    def _get_msg_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self.msg_locks:
            self.msg_locks[chat_id] = asyncio.Lock()
        return self.msg_locks[chat_id]

    def _get_llm_rate_limiter(self, chat_id: int) -> RateLimiter:
        if chat_id not in self.llm_rate_limiters:
            self.llm_rate_limiters[chat_id] = RateLimiter(max_calls=3, period=60.0)
        return self.llm_rate_limiters[chat_id]

    async def _read_text_file_async(self, path: Path) -> str:
        if await asyncio.to_thread(path.exists):
            return await asyncio.to_thread(path.read_text, encoding="utf-8")
        return ""

    async def track_id(
        self, context: CallbackContext, chat_id: int, message_id: int
    ) -> None:
        async with self._get_msg_lock(chat_id):
            ids = context.chat_data.setdefault("message_ids", deque())
            ids.append(message_id)
            while len(ids) > MAX_TRACKED_MESSAGES:
                ids.popleft()

    async def safe_reply(
        self, update: Update, context: CallbackContext, text: str, **kwargs: Any
    ) -> Optional[Message]:
        try:
            sent = await update.message.reply_text(text, **kwargs)
            await self.track_id(context, update.effective_chat.id, sent.message_id)
            return sent
        except Exception as e:
            logger.warning("[safe_reply] Failed to send message: %s", e)
            return None

    async def ask_llm(self, history: list[dict]) -> str:
        if not self.personality_prompt or self.llm_client is None:
            return ""
        for attempt in range(3):
            try:
                response = await self.llm_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "system", "content": self.personality_prompt}]
                    + history,
                    max_tokens=300,
                    temperature=0.9,
                    timeout=15,
                )
                return response.choices[0].message.content.strip()
            except RateLimitError:
                wait = 2 ** attempt
                logger.warning("[LLM] rate limited, retrying in %ds", wait)
                await asyncio.sleep(wait)
            except APITimeoutError:
                if attempt == 2:
                    return "⚠️ AI временно недоступен, попробуй позже."
                logger.warning("[LLM] timeout, retrying...")
                await asyncio.sleep(1)
            except APIConnectionError:
                if attempt == 2:
                    return "⚠️ AI временно недоступен, попробуй позже."
                logger.warning("[LLM] connection error, retrying...")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(
                    "[LLM error] %s: %s", type(e).__name__, e, exc_info=True
                )
                code = (
                    getattr(e, "status_code", None)
                    or getattr(e, "code", None)
                    or getattr(e, "status", None)
                )
                if code in (500, 502, 503, 504):
                    wait = 2 ** attempt
                    logger.warning("[LLM] server error %s, retrying in %ds", code, wait)
                    await asyncio.sleep(wait)
                else:
                    if code:
                        return f"__API_ERR:{code}"
                    return "__API_ERR"
        return "⚠️ Не удалось получить ответ от AI."

    async def track_all_messages(
        self, update: Update, context: CallbackContext
    ) -> None:
        if not update.message:
            return

        chat_id = update.effective_chat.id
        await self.track_id(context, chat_id, update.message.message_id)
        known = context.bot_data.setdefault("known_chats", set())
        known.add(chat_id)
        user = update.message.from_user
        if user and user.username:
            username_map = context.bot_data.setdefault("username_map", {})
            username_map[user.username.lower()] = user.id

        if update.message.text and update.effective_chat.type in ("group", "supergroup") and user:
            sender = f"@{user.username}" if user.username else (user.first_name or "Unknown")
            ctx = context.chat_data.setdefault("chat_context", [])
            ctx.append(f"{sender} says: {update.message.text}")
            context.chat_data["chat_context"] = ctx[-CHAT_CONTEXT_LIMIT:]

        if not user or user.is_bot:
            return
        if update.effective_chat.type not in ("group", "supergroup"):
            return

        if user.id in self.banned_users:
            return

        user_rank = await get_user_rank(update, context, user.id)
        if user_rank <= RANK_ADMIN:
            return

        now = time.time()
        spam = context.chat_data.setdefault("spam_tracker", {})
        ts = spam.setdefault(user.id, [])
        cutoff = now - ANTISPAM_WINDOW
        ts[:] = [t for t in ts if t > cutoff]
        ts.append(now)

        if len(ts) <= ANTISPAM_MSG_LIMIT:
            return

        if update.message.text:
            lower = update.message.text.lower().strip().translate(STRIP_PUNCT)
            if lower in self.phrases or lower.startswith(CHANCE_TRIGGER):
                return
            if update.message.reply_to_message:
                if lower in self.rp_commands:
                    return

        spammer_name = (
            f"@{user.username}" if user.username else user.first_name or str(user.id)
        )

        if len(ts) <= ANTISPAM_MUTE_THRESHOLD:
            warned = context.chat_data.setdefault("spam_warned", {})
            if user.id not in warned:
                warned[user.id] = now
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ {spammer_name}, пожалуйста, не флуди!",
                )
            return

        try:
            await context.bot.restrict_chat_member(
                chat_id,
                user.id,
                permissions=NO_PERMISSIONS,
                until_date=int(now + ANTISPAM_MUTE_DURATION),
            )
        except Exception as e:
            logger.warning("[AntiSpam] не получилось замутить: %s", e)
            return

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅️ Я замутил {spammer_name} за спам.",
        )

    async def handle_nuke(
        self, update: Update, context: CallbackContext, params: str
    ) -> None:
        if not params:
            await self.safe_reply(
                update,
                context,
                "Использование: пибот сотри n, где n - целое положительное число",
            )
            return

        try:
            n = int(params)
            if n <= 0:
                raise ValueError
        except ValueError:
            await self.safe_reply(
                update,
                context,
                "Использование: пибот сотри n, где n - целое положительное число",
            )
            return

        chat_id = update.effective_chat.id
        async with self._get_msg_lock(chat_id):
            ids = context.chat_data.setdefault("message_ids", deque())
            if not ids:
                await self.safe_reply(update, context, "⚠️ Не найдено сообщений")
                return

            n = min(n, len(ids)) + 1
            ids_to_delete = [ids.pop() for _ in range(n)]
            ids_to_delete.reverse()

        if not ids_to_delete:
            await self.safe_reply(update, context, "⚠️ Нет сообщений для удаления")
            return

        total = len(ids_to_delete)
        deleted = 0
        for i in range(0, total, DELETE_BATCH_SIZE):
            batch = ids_to_delete[i : i + DELETE_BATCH_SIZE]
            try:
                await context.bot.delete_messages(chat_id=chat_id, message_ids=batch)
                deleted += len(batch)
            except Exception as e:
                logger.warning(
                    "[Nuke] не удалось удалить %d сообщений: %s", len(batch), e
                )

        # Бот удаляет сообщение и потом пытается на него ответить
        # if deleted > 0:
        #     await self.safe_reply(update, context, f"✅️ Удалено {deleted} сообщений")
        # else:
        #     await self.safe_reply(update, context, "⚠️ Не удалось удалить сообщения")
        #
        if deleted == 0:
            await self.safe_reply(update, context, "⚠️ Не удалось удалить сообщения")

    async def handle_kick(
        self, update: Update, context: CallbackContext, params: str
    ) -> None:
        target = await resolve_user(update, context, params)
        if not target:
            await self.safe_reply(
                update,
                context,
                "⚠️ Кого вышвырнуть? Ответь на сообщение или укажи @username",
            )
            return

        if await target_immune_to_mkb(update, context, target.id):
            await self.safe_reply(
                update, context, "⛔️ Этого пользователя нельзя кикнуть"
            )
            return

        try:
            await context.bot.ban_chat_member(update.effective_chat.id, target.id)
            await context.bot.unban_chat_member(update.effective_chat.id, target.id)
            await self.safe_reply(
                update, context, f"✅️ {get_mention(target)} выкинут за борт"
            )
        except Exception as e:
            await self.safe_reply(update, context, f"⚠️ Ошибка кика: {e}")

    async def handle_ban(
        self, update: Update, context: CallbackContext, params: str
    ) -> None:
        target_str = _strip_gulag_prefix(params)
        if target_str is None:
            target = await resolve_user(update, context, params)
            if target is None:
                await self.safe_reply(
                    update,
                    context,
                    "Использование: пибот выкинь @user",
                )
                return
        else:
            target = await resolve_user(update, context, target_str)
        if not target:
            await self.safe_reply(
                update,
                context,
                "⚠️ Кого банить? Ответь на сообщение или укажи @username",
            )
            return

        if await target_immune_to_mkb(update, context, target.id):
            await self.safe_reply(
                update, context, "⛔️ Этого пользователя нельзя забанить"
            )
            return

        try:
            await context.bot.ban_chat_member(
                update.effective_chat.id, target.id, revoke_messages=True
            )
            self.banned_users.add(target.id)
            context.bot_data["banned_users"] = list(self.banned_users)
            await self.safe_reply(
                update, context, f"✅️ {get_mention(target)} был забанен"
            )
        except Exception as e:
            await self.safe_reply(update, context, f"⚠️ Ошибка бана: {e}")

    async def handle_unban(
        self, update: Update, context: CallbackContext, params: str
    ) -> None:
        target = await resolve_user(update, context, params)
        if not target:
            await self.safe_reply(
                update,
                context,
                "⚠️ Кого разбанить? Ответь на сообщение или укажи @username",
            )
            return

        try:
            await context.bot.unban_chat_member(
                update.effective_chat.id, target.id, only_if_banned=True
            )
            self.banned_users.discard(target.id)
            context.bot_data["banned_users"] = list(self.banned_users)
            await self.safe_reply(
                update, context, f"✅️ {get_mention(target)} возвращён из гулага"
            )
        except Exception as e:
            await self.safe_reply(update, context, f"⚠️ Ошибка разбана: {e}")

    async def handle_block(
        self, update: Update, context: CallbackContext, params: str
    ) -> None:
        if not params:
            await self.safe_reply(
                update,
                context,
                "Использование: пибот заблокируй <id> или @username",
            )
            return

        target = await resolve_user(update, context, params)
        if target:
            target_id = target.id
        else:
            try:
                target_id = int(params.strip())
            except ValueError:
                await self.safe_reply(
                    update, context, "⚠️ Укажи числовой ID или @username"
                )
                return

        self.banned_users.add(target_id)
        context.bot_data["banned_users"] = list(self.banned_users)
        await self.safe_reply(
            update, context, f"✅️ Пользователь {target_id} заблокирован"
        )

    async def handle_mute(
        self, update: Update, context: CallbackContext, params: str
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
            elif update.message.reply_to_message:
                try:
                    duration_minutes = float(parts[0])
                    if duration_minutes >= 0.5:
                        user_params = ""
                except ValueError:
                    pass

        target = await resolve_user(update, context, user_params)
        if not target:
            await self.safe_reply(
                update,
                context,
                "⚠️ Кого мутить? Ответь на сообщение или укажи @username",
            )
            return

        if await target_immune_to_mkb(update, context, target.id):
            await self.safe_reply(
                update, context, "⛔️ Этого пользователя нельзя замутить"
            )
            return

        try:
            if duration_minutes is not None:
                until_date = int(time.time()) + int(duration_minutes * 60)
                await context.bot.restrict_chat_member(
                    update.effective_chat.id,
                    target.id,
                    permissions=NO_PERMISSIONS,
                    until_date=until_date,
                )
                end_str = datetime.fromtimestamp(until_date).strftime("%H:%M")
                await self.safe_reply(
                    update,
                    context,
                    f"✅️ {get_mention(target)} рот прикрой на {duration_minutes} минут (до {end_str})",
                )
            else:
                await context.bot.restrict_chat_member(
                    update.effective_chat.id, target.id, permissions=NO_PERMISSIONS
                )
                await self.safe_reply(
                    update, context, f"✅️ {get_mention(target)} не глаголь тут"
                )
        except Exception as e:
            await self.safe_reply(update, context, f"⚠️ Ошибка мута: {e}")

    async def handle_unmute(
        self, update: Update, context: CallbackContext, params: str
    ) -> None:
        target = await resolve_user(update, context, params)
        if not target:
            await self.safe_reply(
                update,
                context,
                "⚠️ Кого размутить? Ответь на сообщение или укажи @username",
            )
            return

        try:
            await context.bot.restrict_chat_member(
                update.effective_chat.id, target.id, permissions=ALL_PERMISSIONS
            )
            await self.safe_reply(
                update, context, f"✅️ {get_mention(target)} больше не буянь"
            )
        except Exception as e:
            await self.safe_reply(update, context, f"⚠️ Ошибка размута: {e}")

    async def handle_rank(
        self, update: Update, context: CallbackContext, params: str
    ) -> None:
        parts = params.split(maxsplit=2)
        if len(parts) < 1:
            await self.safe_reply(
                update,
                context,
                "Использование: пибот ранг n для @user (n = 2, 3, 4)",
            )
            return

        try:
            new_rank = int(parts[0])
        except ValueError:
            await self.safe_reply(
                update,
                context,
                "Использование: пибот ранг n для @user (n = 2, 3, 4)",
            )
            return

        if new_rank not in (RANK_ADMIN_PLUS, RANK_ADMIN, RANK_MEMBER):
            await self.safe_reply(update, context, "Ранг может быть только 2, 3 или 4")
            return

        target_str = ""
        if len(parts) >= 3 and parts[1] == "для":
            target_str = parts[2]
        elif len(parts) == 2:
            target_str = parts[1]

        target = await resolve_user(update, context, target_str)
        if not target:
            await self.safe_reply(
                update,
                context,
                "⚠️ Кому изменить ранг? Ответь на сообщение или укажи @username",
            )
            return

        if target.id == context.bot.id:
            await self.safe_reply(update, context, "⛔️ Нельзя изменить ранг бота")
            return

        target_rank = await get_user_rank(update, context, target.id)
        if target_rank == RANK_OWNER:
            await self.safe_reply(update, context, "⛔️ Нельзя изменить ранг владельца")
            return

        try:
            target_member = await update.effective_chat.get_member(target.id)
            is_tg_admin = target_member.status == ChatMemberStatus.ADMINISTRATOR
        except Exception:
            is_tg_admin = False

        if is_tg_admin and new_rank == RANK_MEMBER:
            await self.safe_reply(
                update,
                context,
                "⛔️ Нельзя выдать ранг 4 администратору. Понизьте его через Telegram.",
            )
            return

        if not is_tg_admin and new_rank in (RANK_ADMIN_PLUS, RANK_ADMIN):
            await self.safe_reply(
                update,
                context,
                "⛔️ Нельзя выдать ранг 2 или 3 обычному участнику. "
                "Сначала выдайте админку через Telegram.",
            )
            return

        ranks = context.chat_data.setdefault("ranks", {})
        ranks[target.id] = new_rank

        rank_names = {
            RANK_ADMIN_PLUS: "Admin+",
            RANK_ADMIN: "Admin",
            RANK_MEMBER: "Member",
        }
        await self.safe_reply(
            update,
            context,
            f"✅️ Ранг {get_mention(target)} изменён на {rank_names[new_rank]}",
        )

    async def handle_botinfo_cmd(
        self, update: Update, context: CallbackContext, params: str
    ) -> None:
        text = await self._read_text_file_async(BOTINFO_PATH)
        if not text:
            text = "⚠️ Инфа потерялась, проверь путь к моему описанию"
        await self.safe_reply(update, context, text)

    async def handle_changelog_cmd(
        self, update: Update, context: CallbackContext, params: str
    ) -> None:
        text = await self._read_text_file_async(CHANGELOG_PATH)
        if not text:
            text = "⚠️ Инфа потерялась, проверь путь к моим обновам"
        await self.safe_reply(update, context, text)

    async def handle_commands_cmd(
        self, update: Update, context: CallbackContext, params: str
    ) -> None:
        text = await self._read_text_file_async(COMMANDLIST_PATH)
        if not text:
            text = "⚠️ Инфа потерялась, проверь путь к списку команд"
        await self.safe_reply(update, context, text)

    async def handle_rank_list(
        self, update: Update, context: CallbackContext, params: str
    ) -> None:
        ranks = context.chat_data.setdefault("ranks", {})
        rank_names = {RANK_ADMIN_PLUS: "Admin+", RANK_ADMIN: "Admin"}
        lines = []

        for user_id, rank in ranks.items():
            if rank not in (RANK_ADMIN_PLUS, RANK_ADMIN):
                continue
            try:
                member = await update.effective_chat.get_member(user_id)
                display = get_mention(member.user)
            except Exception:
                display = str(user_id)
            lines.append(f"{display} имеет ранг {rank} — {rank_names[rank]}")

        if not lines:
            await self.safe_reply(
                update, context, "Нет пользователей с особыми рангами"
            )
            return

        await self.safe_reply(update, context, "\n".join(lines))

    async def handle_message(self, update: Update, context: CallbackContext) -> None:
        if not self._pre_check(update, context):
            return
        text = update.message.text.strip()
        if await self._handle_command(update, context, text):
            return
        if is_user_ignored(context, update.message.from_user.id):
            return
        lower_text = text.lower().translate(STRIP_PUNCT)
        if await self._handle_rp(update, context, lower_text):
            return
        if await self._handle_phrase(update, context, lower_text):
            return
        if await self._handle_chance(update, context, lower_text):
            return
        await self._handle_llm(update, context, text)

    def _pre_check(self, update: Update, context: CallbackContext) -> bool:
        if not update.message or not update.message.text:
            return False
        if (
            update.message.from_user
            and update.message.from_user.id in self.banned_users
        ):
            return False
        if update.message.date:
            msg_date = update.message.date
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            msg_age = (datetime.now(timezone.utc) - msg_date).total_seconds()
            if msg_age > MAX_MESSAGE_AGE:
                if not update.message.text.lower().startswith(PIBOT_PREFIX):
                    return False
        return True

    async def _handle_command(
        self, update: Update, context: CallbackContext, text: str
    ) -> bool:
        lower = text.lower()
        if not lower.startswith(PIBOT_PREFIX):
            return False
        rest = text[PIBOT_PREFIX_LEN:].strip()
        if not rest:
            return False

        parts = rest.split(maxsplit=1)
        subcommand = parts[0].lower()
        params = parts[1] if len(parts) > 1 else ""

        if subcommand not in self.commands:
            return False

        cmd = self.commands[subcommand]
        user_id = update.message.from_user.id

        if cmd.dev_only:
            if user_id not in self.dev_ids:
                await self.safe_reply(
                    update, context, "⛔️ Недостаточно прав для этой команды"
                )
                return True
        else:
            user_rank = await get_user_rank(update, context, user_id)
            if user_rank > cmd.value:
                await self.safe_reply(
                    update, context, "⛔️ Недостаточно прав для этой команды"
                )
                return True

        await cmd.handler(update, context, params)
        return True

    async def _handle_rp(
        self, update: Update, context: CallbackContext, lower_text: str
    ) -> bool:
        if not (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
        ):
            return False
        if lower_text not in self.rp_commands:
            return False

        if track_trigger_spam(context, update.message.from_user.id, lower_text):
            await self.safe_reply(update, context, "Ой всё", disable_notification=True)
            return True
        if not await self.rate_limiter.acquire():
            return True

        user1 = get_mention(update.message.from_user)
        user2 = get_mention(update.message.reply_to_message.from_user)
        response = (
            self.rp_commands[lower_text]
            .replace("{mention1}", user1)
            .replace("{mention2}", user2)
        )
        await self.safe_reply(update, context, response, disable_notification=True)
        return True

    async def _handle_phrase(
        self, update: Update, context: CallbackContext, lower_text: str
    ) -> bool:
        if lower_text not in self.phrases:
            return False

        if track_trigger_spam(context, update.message.from_user.id, lower_text):
            await self.safe_reply(update, context, "Ой всё", disable_notification=True)
            return True
        if not await self.rate_limiter.acquire():
            return True

        response = self.phrases[lower_text]
        mention = get_mention(update.message.from_user)

        if response in SPECIAL_RESPONSES:
            path, fallback = SPECIAL_RESPONSES[response]
            response = await self._read_text_file_async(path) or fallback

        response = response.replace("{mention}", mention)
        await self.safe_reply(update, context, response, disable_notification=True)
        return True

    async def _handle_chance(
        self, update: Update, context: CallbackContext, lower_text: str
    ) -> bool:
        if not lower_text.startswith(CHANCE_TRIGGER):
            return False

        if track_trigger_spam(context, update.message.from_user.id, lower_text):
            await self.safe_reply(update, context, "Ой всё", disable_notification=True)
            return True
        if not await self.rate_limiter.acquire():
            return True

        n = random.randint(0, 100)
        await self.safe_reply(
            update,
            context,
            f"Я думаю, что вероятность {n}%",
            disable_notification=True,
        )
        return True

    async def _handle_llm(
        self, update: Update, context: CallbackContext, text: str
    ) -> None:
        if not self.llm_client:
            return
        if update.message.from_user.id == context.bot.id:
            return

        is_mentioned = False
        if update.effective_chat.type == "private":
            is_mentioned = True
        elif update.message.entities:
            bot_username = context.bot.username
            for entity in update.message.entities:
                if entity.type == MessageEntity.MENTION:
                    mention = text[entity.offset : entity.offset + entity.length]
                    if mention.lower() == f"@{bot_username.lower()}":
                        is_mentioned = True
                        break

        if not is_mentioned:
            return

        if not await self._get_llm_rate_limiter(update.effective_chat.id).acquire():
            return

        chat_history = context.chat_data.setdefault("llm_history", [])
        history = list(chat_history)
        if update.effective_chat.type in ("group", "supergroup"):
            chat_context = context.chat_data.get("chat_context", [])
            if chat_context:
                history.append({"role": "system", "content": "Recent chat:\n" + "\n".join(chat_context)})
        clean_text = text
        if update.message.entities:
            for entity in sorted(
                update.message.entities, key=lambda e: e.offset, reverse=True
            ):
                if entity.type == MessageEntity.MENTION:
                    clean_text = (
                        clean_text[: entity.offset]
                        + clean_text[entity.offset + entity.length :]
                    )
        clean_text = clean_text.strip()
        if not clean_text:
            return

        history.append({"role": "user", "content": clean_text})
        response_text = await self.ask_llm(history[-LLM_HISTORY_LIMIT:])
        if response_text and response_text.startswith("__API_ERR"):
            parts = response_text.split(":", 1)
            msg = "API не доступен"
            if len(parts) > 1 and parts[1]:
                msg += f" ({parts[1]})"
            await self.safe_reply(update, context, msg, disable_notification=True)
        elif response_text:
            history.append({"role": "assistant", "content": response_text})
            context.chat_data["llm_history"] = history[-LLM_HISTORY_LIMIT:]
            await self.safe_reply(
                update, context, response_text, disable_notification=True
            )

    async def _cleanup_caches(self, context: CallbackContext) -> None:
        known = context.bot_data.get("known_chats", set())
        for cid in list(self.msg_locks):
            if cid not in known:
                del self.msg_locks[cid]
        for cid in list(self.llm_rate_limiters):
            if cid not in known:
                del self.llm_rate_limiters[cid]


def load_config() -> dict[str, str]:
    token = os.getenv("TELEGRAM_TOKEN", "")
    groq_key = os.getenv("GROQ_KEY", "")

    if not token or token == "YOUR-TELEGRAM-TOKEN":
        raise ValueError(
            "❌ Токен не настроен!\n"
            "Вставьте токен в .env (TELEGRAM_TOKEN) (получить у @BotFather)"
        )
    if not groq_key or groq_key == "YOUR-GROQ-API-KEY-HERE":
        raise ValueError(
            "❌ Groq API ключ не настроен!\n"
            "Вставьте ключ в .env (GROQ_KEY) (получить на console.groq.com)"
        )

    return {"token": token, "groq_key": groq_key}


def main() -> None:
    load_dotenv(BASE / ".env")

    try:
        cfg = load_config()
    except ValueError as e:
        logging.basicConfig(level=logging.INFO)
        logger.error(e)
        return

    token = cfg["token"]

    sys.path.insert(0, str(BASE / "important"))
    from logging_settings import setup_logging  # type: ignore[import-untyped]

    setup_logging(logging.INFO, token=token)

    bot = PiBot(token)
    logger.info("PiBot started...")
    bot.run()


if __name__ == "__main__":
    main()
