import logging
import random
import string
import time
from datetime import datetime, timezone

from telegram import Message, MessageEntity, Update
from telegram.ext import CallbackContext

from antispam import (
    rate_limiter,
    is_user_ignored,
    track_trigger_spam,
    get_llm_rate_limiter,
    llm_global_limiter,
)
from auth import (
    get_mention,
    is_banned,
    check_dev_access,
    get_user_rank,
)
from llm_service import ask_llm, llm_client
from registry import PIBOT_COMMANDS
from storage import (
    _banned_users,
    _phrases,
    _rp_commands,
    _cached_botinfo,
    _cached_changelog,
    _cached_commandlist,
    _dev_ids,
    CHANCE_TRIGGER,
    MAX_MESSAGE_AGE,
    LLM_HISTORY_LIMIT,
    MAX_TRACKED_MESSAGES,
    RANK_ADMIN,
    BOTINFO_PATH,
    CHANGELOG_PATH,
    COMMANDLIST_PATH,
    ANTISPAM_MUTE_DURATION,
    NO_PERMISSIONS,
    load_text_file,
)

logger = logging.getLogger(__name__)

STRIP_PUNCT = str.maketrans("", "", string.punctuation)


def track_id(context: CallbackContext, message_id: int) -> None:
    message_ids = context.chat_data.setdefault("message_ids", [])
    message_ids.append(message_id)
    if len(message_ids) > MAX_TRACKED_MESSAGES:
        del message_ids[: MAX_TRACKED_MESSAGES // 2]


async def safe_reply(
    update: Update, context: CallbackContext, text: str, **kwargs: object
) -> Message | None:
    try:
        sent = await update.message.reply_text(text, **kwargs)
        track_id(context, sent.message_id)
        return sent
    except Exception as e:
        logger.warning("[safe_reply] Failed to send message: %s", e)
        return None


async def track_all_messages(update: Update, context: CallbackContext) -> None:
    if not update.message:
        return

    chat_id = update.effective_chat.id
    track_id(context, update.message.message_id)
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

    if user.id in _banned_users:
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

    if not ts:
        del spam[user.id]

    if len(ts) <= 5:
        return

    if update.message.text:
        lower = update.message.text.lower().strip().translate(STRIP_PUNCT)
        if lower in _phrases or lower.startswith(CHANCE_TRIGGER):
            return
        if update.message.reply_to_message:
            if lower in _rp_commands:
                return

    try:
        await context.bot.restrict_chat_member(
            chat_id, user.id, permissions=NO_PERMISSIONS, until_date=int(now + ANTISPAM_MUTE_DURATION)
        )
    except Exception as e:
        logger.warning("[AntiSpam] Failed to mute: %s", e)
        return

    spammer_name = (
        f"@{user.username}" if user.username else user.first_name or str(user.id)
    )

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅️ Я замутил {spammer_name} за спам.",
    )


async def _check_banned(update: Update) -> bool:
    if update.message.from_user and is_banned(update.message.from_user.id):
        return True
    return False


async def _check_age(update: Update, text: str) -> bool:
    if not update.message.date:
        return False
    msg_date = update.message.date
    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)
    msg_age = (datetime.now(timezone.utc) - msg_date).total_seconds()
    if msg_age > MAX_MESSAGE_AGE:
        if not text.lower().startswith("пибот"):
            return True
    return False


async def _handle_command(update: Update, context: CallbackContext, text: str) -> bool:
    if not text.lower().startswith("пибот "):
        return False

    rest = text[6:].strip()
    if not rest:
        return True

    parts = rest.split(maxsplit=1)
    subcommand = parts[0].lower()
    params = parts[1] if len(parts) > 1 else ""

    if subcommand not in PIBOT_COMMANDS:
        return False

    cmd_config = PIBOT_COMMANDS[subcommand]
    user_id = update.message.from_user.id

    if cmd_config.dev_only:
        if not check_dev_access(user_id):
            await safe_reply(update, context, "⛔️ Недостаточно прав для этой команды")
            return True
    else:
        user_rank = await get_user_rank(update, context, user_id)
        if user_rank > cmd_config.value:
            await safe_reply(update, context, "⛔️ Недостаточно прав для этой команды")
            return True

    await cmd_config.handler(update, context, params)
    return True


async def _handle_rp(update: Update, context: CallbackContext, lower_text: str) -> bool:
    if not update.message.reply_to_message or not update.message.reply_to_message.from_user:
        return False
    if lower_text not in _rp_commands:
        return False
    if track_trigger_spam(context, update.message.from_user.id, lower_text):
        await safe_reply(update, context, "Ой всё", disable_notification=True)
        return True
    if not await rate_limiter.acquire():
        return True

    user1 = get_mention(update.message.from_user)
    user2 = get_mention(update.message.reply_to_message.from_user)
    response = (
        _rp_commands[lower_text]
        .replace("{mention1}", user1)
        .replace("{mention2}", user2)
    )
    await safe_reply(update, context, response, disable_notification=True)
    return True


def _get_special_response(key: str) -> str:
    if key == "__botinfo__":
        return _cached_botinfo or load_text_file(BOTINFO_PATH) or "⚠️ Инфа потерялась, проверь путь к моему описанию"
    elif key == "__changelog__":
        return _cached_changelog or load_text_file(CHANGELOG_PATH) or "⚠️ Инфа потерялась, проверь путь к моим обновам"
    elif key == "__commandlist__":
        return _cached_commandlist or load_text_file(COMMANDLIST_PATH) or "⚠️ Инфа потерялась, проверь путь к списку команд"
    return ""


async def _handle_phrase(update: Update, context: CallbackContext, lower_text: str) -> bool:
    if lower_text not in _phrases:
        return False
    if track_trigger_spam(context, update.message.from_user.id, lower_text):
        await safe_reply(update, context, "Ой всё", disable_notification=True)
        return True
    if not await rate_limiter.acquire():
        return True

    response = _phrases[lower_text]
    mention = get_mention(update.message.from_user)

    special = _get_special_response(response)
    if special:
        response = special

    response = response.replace("{mention}", mention)
    await safe_reply(update, context, response, disable_notification=True)
    return True


async def _handle_chance(update: Update, context: CallbackContext, lower_text: str) -> bool:
    if not lower_text.startswith(CHANCE_TRIGGER):
        return False
    if track_trigger_spam(context, update.message.from_user.id, lower_text):
        await safe_reply(update, context, "Ой всё", disable_notification=True)
        return True
    if not await rate_limiter.acquire():
        return True
    n = random.randint(0, 100)
    await safe_reply(
        update,
        context,
        f"Я думаю, что вероятность {n}%",
        disable_notification=True,
    )
    return True


async def _handle_llm(update: Update, context: CallbackContext, text: str) -> bool:
    if not llm_client:
        return False
    if update.message.from_user.id == context.bot.id:
        return False

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
        return False

    if not await llm_global_limiter.acquire():
        return True
    if not await get_llm_rate_limiter(update.effective_chat.id).acquire():
        return True

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
        return True

    history.append({"role": "user", "content": clean_text})
    response_text = await ask_llm(history[-LLM_HISTORY_LIMIT:])
    if response_text and response_text.startswith("__API_ERR"):
        parts = response_text.split(":", 1)
        msg = "API не доступен"
        if len(parts) > 1 and parts[1]:
            msg += f" ({parts[1]})"
        await safe_reply(update, context, msg, disable_notification=True)
    elif response_text:
        history.append({"role": "assistant", "content": response_text})
        context.chat_data["llm_history"] = history[-LLM_HISTORY_LIMIT:]
        await safe_reply(update, context, response_text, disable_notification=True)
    return True


async def handle_message(update: Update, context: CallbackContext) -> None:
    if not update.message or not update.message.text:
        return

    if await _check_banned(update):
        return

    text = update.message.text.strip()
    if not text:
        return

    if await _check_age(update, text):
        return

    if await _handle_command(update, context, text):
        return

    if is_user_ignored(context, update.message.from_user.id):
        return

    lower_text = text.lower().translate(STRIP_PUNCT)

    if await _handle_rp(update, context, lower_text):
        return

    if await _handle_phrase(update, context, lower_text):
        return

    if await _handle_chance(update, context, lower_text):
        return

    await _handle_llm(update, context, text)
