import asyncio
import json
import random
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from openai import AsyncOpenAI
from telegram import (
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
    CommandHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

BASE = Path(__file__).parent.parent
TOKEN_PATH = BASE / "env" / "telegram-token"
PHRASES_PATH = BASE / "bot-data" / "phrases.json"
BOTINFO_PATH = BASE / "bot-data" / "botinfo.md"
CHANGELOG_PATH = BASE / "bot-data" / "changelog.md"
COMMANDLIST_PATH = BASE / "info" / "command-list.md"
RP_COMMANDS_PATH = BASE / "bot-data" / "rp-phrases.json"
BANNED_USERS_PATH = BASE / "bot-data" / "banned-users.json"
DEV_IDS_PATH = BASE / "env" / "dev-ids.json"

MAX_TRACKED_MESSAGES = 1000
DELETE_BATCH_SIZE = 100
MAX_MESSAGE_AGE = 120
TRIGGER_SPAM_WINDOW = 60
TRIGGER_SPAM_LIMIT = 5
TRIGGER_SPAM_MUTE = 120

CHANCE_TRIGGER = "пибот инфа"
WELCOME_MESSAGE = "Я вернулась"

GROQ_KEY_PATH = BASE / "env" / "groq-key"
PERSONALITY_PATH = BASE / "bot-data" / "personality.md"

RANK_OWNER = 1
RANK_ADMIN_PLUS = 2
RANK_ADMIN = 3
RANK_MEMBER = 4

PIBOT_COMMANDS: dict[str, dict[str, Any]] = {}

llm_client: Optional[AsyncOpenAI] = None
personality_prompt: str = ""

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


def pibot_command(
    name: str, value: int, dev_only: bool = False
) -> Callable[[Callable], Callable]:
    def decorator(func: Callable) -> Callable:
        PIBOT_COMMANDS[name] = {"handler": func, "value": value, "dev_only": dev_only}
        return func

    return decorator


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


def load_phrases() -> dict[str, str]:
    if PHRASES_PATH.exists():
        with open(PHRASES_PATH) as f:
            return json.load(f)
    return {}


def load_text_file(path: Path) -> str:
    if path.exists():
        return path.read_text().strip()
    return ""


def load_rp_commands() -> dict[str, str]:
    if RP_COMMANDS_PATH.exists():
        with open(RP_COMMANDS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_banned_users() -> set[int]:
    if BANNED_USERS_PATH.exists():
        with open(BANNED_USERS_PATH) as f:
            return set(json.load(f))
    return set()


def save_banned_users(ids: set[int]) -> None:
    with open(BANNED_USERS_PATH, "w") as f:
        json.dump(sorted(ids), f)


def load_dev_ids() -> set[int]:
    if DEV_IDS_PATH.exists():
        with open(DEV_IDS_PATH) as f:
            return set(json.load(f))
    return set()


def get_mention(user: User) -> str:
    return f"@{user.username}" if user.username else (user.first_name or "User")


def user_display(user: User) -> str:
    return get_mention(user)


rate_limiter = RateLimiter(max_calls=5, period=1.0)

STRIP_PUNCT = str.maketrans("", "", string.punctuation)


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


llm_rate_limiters: dict[int, RateLimiter] = {}


def get_llm_rate_limiter(chat_id: int) -> RateLimiter:
    if chat_id not in llm_rate_limiters:
        llm_rate_limiters[chat_id] = RateLimiter(max_calls=3, period=60.0)
    return llm_rate_limiters[chat_id]


def init_clients() -> None:
    global llm_client
    llm_client = None
    try:
        groq_key = GROQ_KEY_PATH.read_text().strip()
        if groq_key and groq_key != "YOUR-GROQ-API-KEY-HERE":
            llm_client = AsyncOpenAI(
                api_key=groq_key, base_url="https://api.groq.com/openai/v1"
            )
    except Exception as e:
        print(f"[Init] Groq client error: {e}")


async def ask_llm(history: list[dict]) -> str:
    if not personality_prompt:
        return ""
    try:
        response = await llm_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": personality_prompt}] + history,
            max_tokens=300,
            temperature=0.9,
        )
        return response.choices[0].message.content.strip()
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
    return ""


def track_id(context: CallbackContext, chat_id: int, message_id: int) -> None:
    message_ids = context.chat_data.setdefault("message_ids", [])
    message_ids.append(message_id)
    if len(message_ids) > MAX_TRACKED_MESSAGES:
        del message_ids[: MAX_TRACKED_MESSAGES // 2]


async def safe_reply(
    update: Update, context: CallbackContext, text: str, **kwargs: Any
) -> Optional[Message]:
    try:
        sent = await update.message.reply_text(text, **kwargs)
        track_id(context, update.effective_chat.id, sent.message_id)
        return sent
    except Exception:
        return None


async def get_user_rank(update: Update, context: CallbackContext, user_id: int) -> int:
    try:
        member = await update.effective_chat.get_member(user_id)
        if member.status == ChatMemberStatus.OWNER:
            return RANK_OWNER
    except Exception:
        pass

    ranks = context.chat_data.setdefault("ranks", {})
    if user_id in ranks:
        return ranks[user_id]

    try:
        member = await update.effective_chat.get_member(user_id)
        if member.status == ChatMemberStatus.ADMINISTRATOR:
            return RANK_ADMIN
    except Exception:
        pass

    return RANK_MEMBER


async def target_immune_to_mkb(
    update: Update, context: CallbackContext, target_user_id: int
) -> bool:
    rank = await get_user_rank(update, context, target_user_id)
    return rank <= RANK_ADMIN


async def track_all_messages(update: Update, context: CallbackContext) -> None:
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

    if user.id in load_banned_users():
        return

    user_rank = await get_user_rank(update, context, user.id)
    if user_rank <= RANK_ADMIN:
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

    spammer_name = (
        f"@{user.username}" if user.username else user.first_name or str(user.id)
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅️ Я замутил {spammer_name} за спам.",
    )


async def resolve_user(
    update: Update, context: CallbackContext, params: str
) -> Optional[User]:
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


@pibot_command("сотри", 2)
async def handle_nuke(update: Update, context: CallbackContext, params: str) -> None:
    if not params:
        await safe_reply(
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
        await safe_reply(
            update,
            context,
            "Использование: пибот сотри n, где n - целое положительное число",
        )
        return

    chat_id = update.effective_chat.id
    message_ids = context.chat_data.setdefault("message_ids", [])

    if not message_ids:
        await safe_reply(update, context, "⚠️ Не найдено сообщений")
        return

    n = min(n + 1, len(message_ids))
    ids_to_delete = message_ids[-n:]
    del message_ids[-n:]

    for i in range(0, len(ids_to_delete), DELETE_BATCH_SIZE):
        batch = ids_to_delete[i : i + DELETE_BATCH_SIZE]
        try:
            await context.bot.delete_messages(chat_id=chat_id, message_ids=batch)
        except Exception as e:
            await safe_reply(update, context, f"⚠️ Ошибка удаления: {e}")
            break


@pibot_command("кикни", 2)
async def handle_kick(update: Update, context: CallbackContext, params: str) -> None:
    target = await resolve_user(update, context, params)
    if not target:
        await safe_reply(
            update,
            context,
            "⚠️ Кого вышвырнуть? Ответь на сообщение или укажи @username",
        )
        return

    if await target_immune_to_mkb(update, context, target.id):
        await safe_reply(update, context, "⛔️ Этого пользователя нельзя кикнуть")
        return

    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id)
        await context.bot.unban_chat_member(update.effective_chat.id, target.id)
        await safe_reply(update, context, f"✅️ {user_display(target)} выкинут за борт")
    except Exception as e:
        await safe_reply(update, context, f"⚠️ Ошибка кика: {e}")


@pibot_command("кинь", 1)
async def handle_ban(update: Update, context: CallbackContext, params: str) -> None:
    if not params.startswith("в гулаг ") and not params.startswith("вгулаг "):
        await safe_reply(
            update,
            context,
            "Использование: пибот кинь в гулаг @user",
        )
        return

    target_str = params[8:].strip()
    target = await resolve_user(update, context, target_str)
    if not target:
        await safe_reply(
            update, context, "⚠️ Кого банить? Ответь на сообщение или укажи @username"
        )
        return

    if await target_immune_to_mkb(update, context, target.id):
        await safe_reply(update, context, "⛔️ Этого пользователя нельзя забанить")
        return

    try:
        await context.bot.ban_chat_member(
            update.effective_chat.id, target.id, revoke_messages=True
        )
        banned = load_banned_users()
        banned.add(target.id)
        save_banned_users(banned)
        await safe_reply(update, context, f"✅️ {user_display(target)} был забанен")
    except Exception as e:
        await safe_reply(update, context, f"⚠️ Ошибка бана: {e}")


@pibot_command("верни", 1)
async def handle_unban(update: Update, context: CallbackContext, params: str) -> None:
    target = await resolve_user(update, context, params)
    if not target:
        await safe_reply(
            update,
            context,
            "⚠️ Кого разбанить? Ответь на сообщение или укажи @username",
        )
        return

    try:
        await context.bot.unban_chat_member(
            update.effective_chat.id, target.id, only_if_banned=True
        )
        banned = load_banned_users()
        banned.discard(target.id)
        save_banned_users(banned)
        await safe_reply(
            update, context, f"✅️ {user_display(target)} возвращён из гулага"
        )
    except Exception as e:
        await safe_reply(update, context, f"⚠️ Ошибка разбана: {e}")


@pibot_command("заблокируй", 0, dev_only=True)
async def handle_block(update: Update, context: CallbackContext, params: str) -> None:
    if not params:
        await safe_reply(
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
            await safe_reply(update, context, "⚠️ Укажи числовой ID или @username")
            return

    banned = load_banned_users()
    banned.add(target_id)
    save_banned_users(banned)
    await safe_reply(update, context, f"✅️ Пользователь {target_id} заблокирован")


@pibot_command("мут", 3)
async def handle_mute(update: Update, context: CallbackContext, params: str) -> None:
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
        await safe_reply(
            update,
            context,
            "⚠️ Кого мутить? Ответь на сообщение или укажи @username",
        )
        return

    if await target_immune_to_mkb(update, context, target.id):
        await safe_reply(update, context, "⛔️ Этого пользователя нельзя замутить")
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


@pibot_command("размут", 3)
async def handle_unmute(update: Update, context: CallbackContext, params: str) -> None:
    target = await resolve_user(update, context, params)
    if not target:
        await safe_reply(
            update,
            context,
            "⚠️ Кого размутить? Ответь на сообщение или укажи @username",
        )
        return

    if await target_immune_to_mkb(update, context, target.id):
        await safe_reply(update, context, "⛔️ Этого пользователя нельзя размутить")
        return

    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id, permissions=ALL_PERMISSIONS
        )
        await safe_reply(update, context, f"✅️ {user_display(target)} больше не буянь")
    except Exception as e:
        await safe_reply(update, context, f"⚠️ Ошибка размута: {e}")


@pibot_command("ранг", 1)
async def handle_rank(update: Update, context: CallbackContext, params: str) -> None:
    parts = params.split(maxsplit=2)
    if len(parts) < 1:
        await safe_reply(
            update,
            context,
            "Использование: пибот ранг n для @user (n = 2, 3, 4)",
        )
        return

    try:
        new_rank = int(parts[0])
    except ValueError:
        await safe_reply(
            update,
            context,
            "Использование: пибот ранг n для @user (n = 2, 3, 4)",
        )
        return

    if new_rank not in (RANK_ADMIN_PLUS, RANK_ADMIN, RANK_MEMBER):
        await safe_reply(update, context, "Ранг может быть только 2, 3 или 4")
        return

    target_str = ""
    if len(parts) >= 3 and parts[1] == "для":
        target_str = parts[2]
    elif len(parts) == 2:
        target_str = parts[1]

    target = await resolve_user(update, context, target_str)
    if not target:
        await safe_reply(
            update,
            context,
            "⚠️ Кому изменить ранг? Ответь на сообщение или укажи @username",
        )
        return

    target_rank = await get_user_rank(update, context, target.id)
    if target_rank == RANK_OWNER:
        await safe_reply(update, context, "⛔️ Нельзя изменить ранг владельца")
        return

    ranks = context.chat_data.setdefault("ranks", {})
    ranks[target.id] = new_rank

    rank_names = {2: "Admin+", 3: "Admin", 4: "Member"}
    await safe_reply(
        update,
        context,
        f"✅️ Ранг {user_display(target)} изменён на {rank_names[new_rank]}",
    )


@pibot_command("био", 4)
async def handle_botinfo_cmd(
    update: Update, context: CallbackContext, params: str
) -> None:
    text = load_text_file(BOTINFO_PATH)
    if not text:
        text = "⚠️ Инфа потерялась, проверь путь к моему описанию"
    await safe_reply(update, context, text)


@pibot_command("обновы", 4)
async def handle_changelog_cmd(
    update: Update, context: CallbackContext, params: str
) -> None:
    text = load_text_file(CHANGELOG_PATH)
    if not text:
        text = "⚠️ Инфа потерялась, проверь путь к моим обновам"
    await safe_reply(update, context, text)


@pibot_command("команды", 4)
async def handle_commands_cmd(
    update: Update, context: CallbackContext, params: str
) -> None:
    text = load_text_file(COMMANDLIST_PATH)
    if not text:
        text = "⚠️ Инфа потерялась, проверь путь к списку команд"
    await safe_reply(update, context, text)


@pibot_command("ранги", 4)
async def handle_rank_list(
    update: Update, context: CallbackContext, params: str
) -> None:
    ranks = context.chat_data.setdefault("ranks", {})
    rank_names = {2: "Admin+", 3: "Admin"}
    lines = []

    for user_id, rank in ranks.items():
        if rank not in (2, 3):
            continue
        try:
            member = await update.effective_chat.get_member(user_id)
            display = user_display(member.user)
        except Exception:
            display = str(user_id)
        lines.append(f"{display} имеет ранг {rank} — {rank_names[rank]}")

    if not lines:
        await safe_reply(update, context, "Нет пользователей с особыми рангами")
        return

    await safe_reply(update, context, "\n".join(lines))


async def handle_message(update: Update, context: CallbackContext) -> None:
    if not update.message or not update.message.text:
        return

    if update.message.from_user and update.message.from_user.id in load_banned_users():
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
            if not text.lower().startswith("пибот"):
                return

    if text.lower().startswith("пибот "):
        rest = text[6:].strip()
        if not rest:
            return

        parts = rest.split(maxsplit=1)
        subcommand = parts[0].lower()
        params = parts[1] if len(parts) > 1 else ""

        if subcommand in PIBOT_COMMANDS:
            cmd_config = PIBOT_COMMANDS[subcommand]
            handler = cmd_config["handler"]
            required_value = cmd_config["value"]
            dev_only = cmd_config.get("dev_only", False)

            user_id = update.message.from_user.id

            if dev_only and user_id in load_dev_ids():
                pass
            else:
                user_rank = await get_user_rank(update, context, user_id)
                if user_rank > required_value:
                    await safe_reply(
                        update, context, "⛔️ Недостаточно прав для этой команды"
                    )
                    return

            await handler(update, context, params)
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
            response = load_text_file(BOTINFO_PATH)
            if not response:
                response = "⚠️ Инфа потерялась, проверь путь к моему описанию"

        if response == "__changelog__":
            response = load_text_file(CHANGELOG_PATH)
            if not response:
                response = "⚠️ Инфа потерялась, проверь путь к моим обновам"

        if response == "__commandlist__":
            response = load_text_file(COMMANDLIST_PATH)
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

    if not llm_client:
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


def welcome_text() -> str:
    return f"{WELCOME_MESSAGE}"  # Don't add AI backend name


async def start(update: Update, context: CallbackContext) -> None:
    await safe_reply(update, context, welcome_text())


async def startup_notify(context: CallbackContext) -> None:
    known = context.bot_data.get("known_chats", set())
    for chat_id in known:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=welcome_text(),
            )
        except Exception:
            pass


async def post_init(app: Application) -> None:
    app.job_queue.run_once(startup_notify, when=3)
    for chat_data in app.chat_data.values():
        chat_data.pop("llm_history", None)
    init_clients()


def main() -> None:
    global personality_prompt

    token = TOKEN_PATH.read_text().strip()
    if not token or token == "YOUR-TELEGRAM-TOKEN":
        print("Пожалуйста вставьте токен бота в telegram-token")
        return

    if PERSONALITY_PATH.exists():
        personality_prompt = PERSONALITY_PATH.read_text().strip()
    else:
        print("personality.md не найден. ИИ выключен")

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
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    print("PiBot started...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
