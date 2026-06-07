import asyncio
import json
import random
import string
import time
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types as genai_types
from openai import AsyncOpenAI
from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageEntity,
    Update,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

BASE = Path(__file__).parent.parent
TOKEN_PATH = BASE / "env" / "telegram-token"
PHRASES_PATH = BASE / "configs" / "phrases.json"
BOTINFO_PATH = BASE / "info" / "botinfo.md"
CHANGELOG_PATH = BASE / "info" / "changelog.md"
COMMANDLIST_PATH = BASE / "info" / "command-list.md"
SYNONYMS_PATH = BASE / "configs" / "synonyms.json"
SUPERUSERS_PATH = BASE / "configs" / "superusers.json"
RP_COMMANDS_PATH = BASE / "configs" / "rp-commands.json"

MAX_TRACKED_MESSAGES = 1000
DELETE_BATCH_SIZE = 100
MAX_MESSAGE_AGE = 120  # 2 minutes in seconds
TRIGGER_SPAM_WINDOW = 60  # seconds
TRIGGER_SPAM_LIMIT = 5
TRIGGER_SPAM_MUTE = 120  # seconds to ignore user after spam

COMMAND_PREFIX = "$"
CHANCE_TRIGGER = "пибот инфа"
CHANCE_TRIGGER_EN = "pibot info"

USE_GROQ = True
USE_GEMINI = False

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_KEY_PATH = BASE / "env" / "gemini-key"
GROQ_KEY_PATH = BASE / "env" / "groq-key"
PERSONALITY_PATH = BASE / "bot-data" / "personality.md"

COMMANDS = {}

llm_client = None
gemini_client = None
personality_prompt = ""

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


def command(name, admin_command=False, superuser_command=False):
    def decorator(func):
        COMMANDS[name] = {
            "handler": func,
            "admin-command": admin_command,
            "superuser-command": superuser_command,
        }
        return func

    return decorator


class RateLimiter:
    def __init__(self, max_calls=5, period=1.0):
        self.max_calls = max_calls
        self.period = period
        self.tokens = float(max_calls)
        self.last_refill = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self):
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


def load_phrases():
    if PHRASES_PATH.exists():
        with open(PHRASES_PATH) as f:
            return json.load(f)
    return {}


def load_botinfo():
    if BOTINFO_PATH.exists():
        return BOTINFO_PATH.read_text().strip()
    return ""


def load_changelog():
    if CHANGELOG_PATH.exists():
        return CHANGELOG_PATH.read_text().strip()
    return ""


def load_commandlist():
    if COMMANDLIST_PATH.exists():
        return COMMANDLIST_PATH.read_text().strip()
    return ""


def load_synonyms() -> dict[str, str]:
    if not SYNONYMS_PATH.exists():
        return {}
    with open(SYNONYMS_PATH) as f:
        groups = json.load(f)
    mapping = {}
    for canonical, aliases in groups.items():
        for alias in aliases:
            mapping[alias.lower()] = canonical.lower()
    return mapping


def load_superusers() -> set[int]:
    if SUPERUSERS_PATH.exists():
        with open(SUPERUSERS_PATH) as f:
            return set(json.load(f))
    return {934151958}


def load_rp_commands() -> dict:
    if RP_COMMANDS_PATH.exists():
        with open(RP_COMMANDS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def get_mention(user):
    return f"@{user.username}" if user.username else (user.first_name or "User")


def user_display(user):
    return get_mention(user)


rate_limiter = RateLimiter(max_calls=5, period=1.0)

STRIP_PUNCT = str.maketrans("", "", string.punctuation)


def is_user_ignored(context, user_id: int) -> bool:
    ignored = context.chat_data.get("ignored_until", {})
    expiry = ignored.get(user_id)
    if expiry is None:
        return False
    if time.time() >= expiry:
        del ignored[user_id]
        return False
    return True


def track_trigger_spam(context, user_id: int, phrase: str) -> bool:
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


llm_rate_limiters = {}


def get_llm_rate_limiter(chat_id: int) -> RateLimiter:
    if chat_id not in llm_rate_limiters:
        llm_rate_limiters[chat_id] = RateLimiter(max_calls=3, period=60.0)
    return llm_rate_limiters[chat_id]


def init_clients():
    global llm_client, gemini_client
    llm_client = None
    gemini_client = None
    try:
        if USE_GROQ:
            groq_key = GROQ_KEY_PATH.read_text().strip()
            if groq_key and groq_key != "YOUR-GROQ-API-KEY-HERE":
                llm_client = AsyncOpenAI(
                    api_key=groq_key, base_url="https://api.groq.com/openai/v1"
                )
    except Exception as e:
        print(f"[Init] Groq client error: {e}")
    try:
        if USE_GEMINI:
            gemini_key = GEMINI_KEY_PATH.read_text().strip()
            if gemini_key and gemini_key != "YOUR-GEMINI-API-KEY-HERE":
                gemini_client = genai.Client(api_key=gemini_key)
    except Exception as e:
        print(f"[Init] Gemini client error: {e}")


async def ask_llm(history: list[dict]) -> str:
    if not personality_prompt:
        return ""
    try:
        if USE_GROQ:
            response = await llm_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": personality_prompt}] + history,
                max_tokens=300,
                temperature=0.9,
            )
            return response.choices[0].message.content.strip()
        elif USE_GEMINI:
            contents = []
            for msg in history:
                role = "user" if msg["role"] == "user" else "model"
                contents.append(
                    genai_types.Content(
                        role=role,
                        parts=[genai_types.Part.from_text(text=msg["content"])],
                    )
                )
            response = await gemini_client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction=personality_prompt
                ),
            )
            return response.text.strip()
    except Exception as e:
        code = (
            getattr(e, "status_code", None)
            or getattr(e, "code", None)
            or getattr(e, "status", None)
        )
        print(f"[LLM error] {type(e).__name__}: {e}")
        if code:
            return f"__API_ERR:{code}"
        return "__API_ERR"


def track_id(context, chat_id: int, message_id: int):
    message_ids = context.chat_data.setdefault("message_ids", [])
    message_ids.append(message_id)
    if len(message_ids) > MAX_TRACKED_MESSAGES:
        del message_ids[: MAX_TRACKED_MESSAGES // 2]


async def safe_reply(update: Update, context, text: str, **kwargs):
    try:
        sent = await update.message.reply_text(text, **kwargs)
        track_id(context, update.effective_chat.id, sent.message_id)
        return sent
    except Exception:
        pass


async def track_all_messages(update: Update, context):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    track_id(context, chat_id, update.message.message_id)
    known = context.bot_data.setdefault("known_chats", set())
    known.add(chat_id)
    user = update.message.from_user
    if user and user.username:
        username_map = context.bot_data.setdefault("username_map", {})
        username_map[user.username.lower()] = user.id

    if not user or user.is_bot:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return
    if user.id in load_superusers():
        return
    try:
        member = await update.effective_chat.get_member(user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return
    except Exception:
        return

    now = time.time()
    spam = context.chat_data.setdefault("spam_tracker", {})
    ts = spam.setdefault(user.id, [])
    cutoff = now - 1.0
    ts[:] = [t for t in ts if t > cutoff]
    ts.append(now)

    if len(ts) <= 5:
        return

    if update.message.text:
        lower = update.message.text.lower().strip().translate(STRIP_PUNCT)
        phrases = load_phrases()
        if lower in phrases or lower.startswith(CHANCE_TRIGGER):
            return
        if update.message.reply_to_message:
            rp_commands = load_rp_commands()
            if lower in rp_commands:
                return

    try:
        await context.bot.restrict_chat_member(
            chat_id, user.id, permissions=NO_PERMISSIONS, until_date=int(now + 60)
        )
    except Exception as e:
        print(f"⛔️ [AntiSpam] не получилось замутить: {e}")
        return

    try:
        admins = await context.bot.get_chat_administrators(chat_id)
    except Exception as e:
        print(f"⛔️ [AntiSpam] не удалось получить список админов: {e}")
        return

    admin_parts = []
    for a in admins:
        if a.user.is_bot:
            continue
        if a.user.username:
            admin_parts.append(f"@{a.user.username}")
        elif a.user.first_name:
            admin_parts.append(
                f'<a href="tg://user?id={a.user.id}">{a.user.first_name}</a>'
            )
        else:
            admin_parts.append(f'<a href="tg://user?id={a.user.id}">{a.user.id}</a>')

    spammer_name = (
        f"@{user.username}" if user.username else user.first_name or str(user.id)
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅️ Я замутил {spammer_name} за спам.\n{' '.join(admin_parts)}",
        parse_mode="HTML",
    )


async def is_admin_user(chat, user_id: int) -> bool:
    try:
        member = await chat.get_member(user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


async def is_admin_cmd(update: Update, context):
    user = update.message.from_user
    return await is_admin_user(update.effective_chat, user.id)


async def resolve_user(update: Update, context, params: str):
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        return update.message.reply_to_message.from_user

    if update.message.entities:
        for entity in update.message.entities:
            if entity.type == MessageEntity.TEXT_MENTION:
                return entity.user
            elif entity.type == MessageEntity.MENTION:
                start = entity.offset
                end = entity.offset + entity.length
                mention_text = update.message.text[start:end]
                username = mention_text[1:].lower()
                username_map = context.bot_data.get("username_map", {})
                if username in username_map:
                    user_id = username_map[username]
                    try:
                        member = await update.effective_chat.get_member(user_id)
                        return member.user
                    except Exception:
                        pass

    if params:
        username = params.strip().lstrip("@").lower()
        username_map = context.bot_data.get("username_map", {})
        if username in username_map:
            user_id = username_map[username]
            try:
                member = await update.effective_chat.get_member(user_id)
                return member.user
            except Exception:
                pass

    return None


async def target_immune(update: Update, target_user) -> bool:
    if target_user.id in load_superusers():
        return True
    try:
        member = await update.effective_chat.get_member(target_user.id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


@command("nuke", admin_command=True)
async def handle_nuke(update: Update, context, params: str):
    if not params:
        await safe_reply(
            update, context, "Использование: $nuke n, где n - целое положительное число"
        )
        return

    try:
        n = int(params)
        if n <= 0:
            raise ValueError
    except ValueError:
        await safe_reply(
            update, context, "Использование: $nuke n, где n - целое положительное число"
        )
        return

    chat_id = update.effective_chat.id
    message_ids = context.chat_data.setdefault("message_ids", [])

    if not message_ids:
        await safe_reply(update, context, "⚠️ Не найдено сообщений")
        return

    n = min(n, len(message_ids))
    ids_to_delete = message_ids[-n:]
    del message_ids[-n:]

    for i in range(0, len(ids_to_delete), DELETE_BATCH_SIZE):
        batch = ids_to_delete[i : i + DELETE_BATCH_SIZE]
        try:
            await context.bot.delete_messages(chat_id=chat_id, message_ids=batch)
        except Exception as e:
            await safe_reply(update, context, f"⚠️ Ошибка удаления: {e}")
            break


@command("kick", superuser_command=True)
async def handle_kick(update: Update, context, params: str):
    target = await resolve_user(update, context, params)
    if not target:
        await safe_reply(
            update,
            context,
            "⚠️ Кого вышвырнуть? Ответь на сообщение или укажи @username",
        )
        return

    if await target_immune(update, target):
        await safe_reply(update, context, "⛔️ Админов кикать нельзя")
        return

    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        await safe_reply(update, context, f"✅️ {user_display(target)} выкинут за борт")
    except Exception as e:
        await safe_reply(update, context, f"⚠️ Ошибка кика: {e}")


@command("ban", superuser_command=True)
async def handle_ban(update: Update, context, params: str):
    target = await resolve_user(update, context, params)
    if not target:
        await safe_reply(
            update, context, "⚠️ Кого банить? Ответь на сообщение или укажи @username"
        )
        return

    if await target_immune(update, target):
        await safe_reply(update, context, "⛔️ Админов банить нельзя")
        return

    try:
        await context.bot.ban_chat_member(
            update.effective_chat.id, target.id, revoke_messages=True
        )
        await safe_reply(update, context, f"✅️ {user_display(target)} был забанен")
    except Exception as e:
        await safe_reply(update, context, f"⚠️ Ошибка бана: {e}")


@command("mute", admin_command=True)
async def handle_mute(update: Update, context, params: str):
    duration_minutes = None
    user_params = params

    if params:
        parts = params.rsplit(maxsplit=1)
        if len(parts) == 2:
            try:
                duration_minutes = int(parts[1])
                if duration_minutes > 0:
                    user_params = parts[0]
                else:
                    duration_minutes = None
            except ValueError:
                pass
        elif update.message.reply_to_message:
            try:
                duration_minutes = int(parts[0])
                if duration_minutes > 0:
                    user_params = ""
            except ValueError:
                pass

    target = await resolve_user(update, context, user_params)
    if not target:
        await safe_reply(
            update, context, "⚠️ Кого мутить? Ответь на сообщение или укажи @username"
        )
        return

    if await target_immune(update, target):
        await safe_reply(update, context, "⛔️ Админов мутить нельзя")
        return

    try:
        if duration_minutes is not None:
            until_date = int(time.time()) + duration_minutes * 60
            await context.bot.restrict_chat_member(
                update.effective_chat.id,
                target.id,
                permissions=NO_PERMISSIONS,
                until_date=until_date,
            )
            end_str = datetime.fromtimestamp(until_date).strftime("%H:%M")
            await safe_reply(
                update,
                context,
                f"✅️ {user_display(target)} рот прикрой на {duration_minutes} минут (до {end_str})",
            )
        else:
            await context.bot.restrict_chat_member(
                update.effective_chat.id, target.id, permissions=NO_PERMISSIONS
            )
            await safe_reply(
                update, context, f"✅️ {user_display(target)} не глаголь тут"
            )
    except Exception as e:
        await safe_reply(update, context, f"⚠️ Ошибка мута: {e}")


@command("unmute", admin_command=True)
async def handle_unmute(update: Update, context, params: str):
    target = await resolve_user(update, context, params)
    if not target:
        await safe_reply(
            update, context, "⚠️ Кого размутить? Ответь на сообщение или укажи @username"
        )
        return

    if await target_immune(update, target):
        await safe_reply(update, context, "⛔️ Админов размутить нельзя")
        return

    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id, permissions=ALL_PERMISSIONS
        )
        await safe_reply(update, context, f"✅️ {user_display(target)} больше не буянь")
    except Exception as e:
        await safe_reply(update, context, f"⚠️ Ошибка размута: {e}")


@command("changeai", superuser_command=True)
async def handle_changeai(update: Update, context, params: str):
    backend = "Gemini" if USE_GEMINI else "Groq"
    await safe_reply(
        update,
        context,
        f"Текущий AI бэкенд: {backend}",
        reply_markup=backend_markup(),
    )


async def handle_message(update: Update, context):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text:
        return

    if update.message.date:
        msg_date = update.message.date
        if msg_date.tzinfo is None:
            msg_date = msg_date.replace(tzinfo=timezone.utc)
        msg_age = (datetime.now(timezone.utc) - msg_date).total_seconds()
        if msg_age > MAX_MESSAGE_AGE:
            if text.startswith(COMMAND_PREFIX):
                pass
            else:
                return

    if text.startswith(COMMAND_PREFIX):
        rest = text[len(COMMAND_PREFIX) :].strip()
        if not rest:
            return

        parts = rest.split(maxsplit=1)
        alias = parts[0].lower()
        params = parts[1] if len(parts) > 1 else ""

        synonyms = load_synonyms()
        canonical = synonyms.get(alias) or alias

        if canonical in COMMANDS:
            cmd_config = COMMANDS[canonical]
            user_id = update.message.from_user.id
            is_super = user_id in load_superusers()

            if not is_super:
                if cmd_config.get("superuser-command"):
                    await safe_reply(
                        update, context, "⛔️ Фиг тебе, это только для суперюзеров"
                    )
                    return
                elif cmd_config.get("admin-command") and not await is_admin_cmd(
                    update, context
                ):
                    await safe_reply(
                        update, context, "⛔️ Фиг тебе, это только для админов"
                    )
                    return

            await cmd_config["handler"](update, context, params)
            return

        return

    if is_user_ignored(context, update.message.from_user.id):
        return

    lower_text = text.lower().translate(STRIP_PUNCT)

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        rp_commands = load_rp_commands()
        if lower_text in rp_commands:
            if track_trigger_spam(context, update.message.from_user.id, lower_text):
                await safe_reply(update, context, "Ой всё", disable_notification=True)
                return
            if not await rate_limiter.acquire():
                return
            user1 = get_mention(update.message.from_user)
            user2 = get_mention(update.message.reply_to_message.from_user)
            response = (
                rp_commands[lower_text]
                .replace("{mention1}", user1)
                .replace("{mention2}", user2)
            )
            await safe_reply(update, context, response, disable_notification=True)
            return

    phrases = load_phrases()

    if lower_text in phrases:
        if track_trigger_spam(context, update.message.from_user.id, lower_text):
            await safe_reply(update, context, "Ой всё", disable_notification=True)
            return
        if not await rate_limiter.acquire():
            return

        response = phrases[lower_text]
        mention = get_mention(update.message.from_user)

        if response == "__botinfo__":
            response = load_botinfo()
            if not response:
                response = "⚠️ Инфа потерялась, проверь путь к моему описанию"

        if response == "__changelog__":
            response = load_changelog()
            if not response:
                response = "⚠️ Инфа потерялась, проверь путь к моим обновам"

        if response == "__commandlist__":
            response = load_commandlist()
            if not response:
                response = "⚠️ Инфа потерялась, проверь путь к списку команд"

        response = response.replace("{mention}", mention)
        await safe_reply(update, context, response, disable_notification=True)
        return

    if lower_text.startswith(CHANCE_TRIGGER):
        if track_trigger_spam(context, update.message.from_user.id, lower_text):
            await safe_reply(update, context, "Ой всё", disable_notification=True)
            return
        if not await rate_limiter.acquire():
            return
        n = random.randint(0, 100)
        await safe_reply(
            update,
            context,
            f"Я думаю, что вероятность {n}%",
            disable_notification=True,
        )
        return

    if (USE_GROQ and not llm_client) or (USE_GEMINI and not gemini_client):
        return

    if update.message.from_user.id == context.bot.id:
        return

    if update.message.from_user.id not in load_superusers():
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

    if not await get_llm_rate_limiter(update.effective_chat.id).acquire():
        return

    chat_history = context.chat_data.setdefault("llm_history", [])
    history = list(chat_history)
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
    response_text = await ask_llm(history[-20:])
    if response_text and response_text.startswith("__API_ERR"):
        parts = response_text.split(":", 1)
        msg = "API не доступен"
        if len(parts) > 1 and parts[1]:
            msg += f" ({parts[1]})"
        await safe_reply(update, context, msg, disable_notification=True)
    elif response_text:
        history.append({"role": "assistant", "content": response_text})
        context.chat_data["llm_history"] = history[-20:]
        await safe_reply(update, context, response_text, disable_notification=True)


def backend_markup():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Use Groq API", callback_data="groq"),
                InlineKeyboardButton("Use Gemini API", callback_data="gemini"),
            ]
        ]
    )


async def start(update: Update, context):
    backend = "Gemini" if USE_GEMINI else "Groq"
    await safe_reply(update, context, f"Я в системе 😎\n\nТекущий AI бэкенд: {backend}")


async def handle_backend_switch(update: Update, context):
    query = update.callback_query

    global USE_GROQ, USE_GEMINI
    backend = query.data
    current = "groq" if USE_GROQ else "gemini"
    if backend == current:
        await query.answer("Уже выбран этот бэкенд")
        return

    USE_GROQ = backend == "groq"
    USE_GEMINI = backend == "gemini"
    init_clients()
    context.bot_data["llm_backend"] = backend

    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass


async def startup_notify(context):
    known = context.bot_data.get("known_chats", set())
    for chat_id in known:
        try:
            backend = "Gemini" if USE_GEMINI else "Groq"
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Я в системе 😎\n\nТекущий AI бэкенд: {backend}",
            )
        except Exception:
            pass


async def post_init(app: Application):
    app.job_queue.run_once(startup_notify, when=3)
    for chat_data in app.chat_data.values():
        chat_data.pop("llm_history", None)

    global USE_GROQ, USE_GEMINI
    backend = app.bot_data.get("llm_backend", "groq")
    USE_GROQ = backend == "groq"
    USE_GEMINI = backend == "gemini"
    init_clients()


def main():
    global personality_prompt

    token = TOKEN_PATH.read_text().strip()
    if not token or token == "YOUR-TELEGRAM-TOKEN":
        print("Please set your bot token in telegram-token")
        return

    if PERSONALITY_PATH.exists():
        personality_prompt = PERSONALITY_PATH.read_text().strip()
    else:
        print("personality.md not found — AI chatbot disabled")

    persistence = PicklePersistence(filepath=Path(__file__).parent / "bot_data.pickle")
    app = (
        Application.builder()
        .token(token)
        .persistence(persistence)
        .post_init(post_init)
        .build()
    )
    app.add_handler(MessageHandler(filters.ALL, track_all_messages), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        CallbackQueryHandler(handle_backend_switch, pattern="^(groq|gemini)$")
    )
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    print("PiBot started...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
