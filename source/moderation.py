import logging
import time
from datetime import datetime
from typing import Optional

from telegram import Update
from telegram.ext import CallbackContext

from message_handlers import safe_reply
from auth import (
    get_user_rank,
    target_immune_to_mkb,
    get_mention,
    resolve_user,
)

from registry import pibot_command

from storage import (
    _banned_users,
    _cached_botinfo,
    _cached_changelog,
    _cached_commandlist,
    save_banned_users,
    ban_lock,
    RANK_OWNER,
    RANK_ADMIN_PLUS,
    RANK_ADMIN,
    RANK_MEMBER,
    RANK_NAMES,
    DELETE_BATCH_SIZE,
    BOTINFO_PATH,
    CHANGELOG_PATH,
    COMMANDLIST_PATH,
    load_text_file,
    NO_PERMISSIONS,
    ALL_PERMISSIONS,
)


logger = logging.getLogger(__name__)


def _parse_duration(params: str, has_reply: bool) -> tuple[Optional[float], str]:
    duration_minutes: Optional[float] = None
    user_params = params

    if not params:
        return None, params

    parts = params.rsplit(maxsplit=1)
    if len(parts) == 2:
        try:
            duration_minutes = float(parts[1])
            if duration_minutes >= 0.5:
                return duration_minutes, parts[0]
        except ValueError:
            pass

    if has_reply:
        try:
            duration_minutes = float(parts[0])
            if duration_minutes >= 0.5:
                return duration_minutes, ""
        except ValueError:
            pass

    return None, params


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
        await safe_reply(update, context, f"✅️ {get_mention(target)} выкинут за борт")
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
        async with ban_lock:
            _banned_users.add(target.id)
            save_banned_users(_banned_users)
        await safe_reply(update, context, f"✅️ {get_mention(target)} был забанен")
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
        
        async with ban_lock:
            _banned_users.discard(target.id)
            save_banned_users(_banned_users)
            
        await safe_reply(
            update, context, f"✅️ {get_mention(target)} возвращён из гулага"
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

    async with ban_lock:
        _banned_users.add(target_id)
        save_banned_users(_banned_users)
    await safe_reply(update, context, f"✅️ Пользователь {target_id} заблокирован")


@pibot_command("мут", 3)
async def handle_mute(update: Update, context: CallbackContext, params: str) -> None:
    has_reply = update.message.reply_to_message is not None
    duration_minutes, user_params = _parse_duration(params, has_reply)

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
                f"✅️ {get_mention(target)} рот прикрой на {duration_minutes} минут (до {end_str})",
            )
        else:
            await context.bot.restrict_chat_member(
                update.effective_chat.id, target.id, permissions=NO_PERMISSIONS
            )
            await safe_reply(
                update, context, f"✅️ {get_mention(target)} не глаголь тут"
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
        await safe_reply(update, context, f"✅️ {get_mention(target)} больше не буянь")
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

    await safe_reply(
        update,
        context,
        f"✅️ Ранг {get_mention(target)} изменён на {RANK_NAMES[new_rank]}",
    )


@pibot_command("био", 4)
async def handle_botinfo_cmd(
    update: Update, context: CallbackContext, params: str
) -> None:
    text = _cached_botinfo or load_text_file(BOTINFO_PATH)
    await safe_reply(update, context, text or "⚠️ Инфа потерялась, проверь путь к моему описанию")


@pibot_command("обновы", 4)
async def handle_changelog_cmd(
    update: Update, context: CallbackContext, params: str
) -> None:
    text = _cached_changelog or load_text_file(CHANGELOG_PATH)
    await safe_reply(update, context, text or "⚠️ Инфа потерялась, проверь путь к моим обновам")


@pibot_command("команды", 4)
async def handle_commands_cmd(
    update: Update, context: CallbackContext, params: str
) -> None:
    text = _cached_commandlist or load_text_file(COMMANDLIST_PATH)
    await safe_reply(update, context, text or "⚠️ Инфа потерялась, проверь путь к списку команд")


@pibot_command("ранги", 4)
async def handle_rank_list(
    update: Update, context: CallbackContext, params: str
) -> None:
    ranks = context.chat_data.setdefault("ranks", {})
    lines = []

    for user_id, rank in ranks.items():
        if rank not in (RANK_ADMIN_PLUS, RANK_ADMIN):
            continue
        try:
            member = await update.effective_chat.get_member(user_id)
            display = get_mention(member.user)
        except Exception:
            display = str(user_id)
        lines.append(f"{display} имеет ранг {rank} — {RANK_NAMES[rank]}")

    if not lines:
        await safe_reply(update, context, "Нет пользователей с особыми рангами")
        return

    await safe_reply(update, context, "\n".join(lines))
