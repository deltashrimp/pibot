import logging

from telegram import (
    MessageEntity,
    Update,
    User,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import CallbackContext

from storage import (
    _banned_users,
    _dev_ids,
    RANK_OWNER,
    RANK_ADMIN,
    RANK_MEMBER,
)

logger = logging.getLogger(__name__)


def get_mention(user: User) -> str:
    return f"@{user.username}" if user.username else (user.first_name or "User")


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
    username: str, update: Update, context: CallbackContext
) -> User | None:
    clean = username.strip().lstrip("@").lower()
    username_map = context.bot_data.get("username_map", {})
    user_id = username_map.get(clean)
    if user_id is None:
        return None
    try:
        member = await update.effective_chat.get_member(user_id)
        return member.user
    except Exception:
        return None


async def resolve_user(
    update: Update, context: CallbackContext, params: str
) -> User | None:
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
                result = await _resolve_by_username(mention_text[1:], update, context)
                if result:
                    return result

    if params:
        return await _resolve_by_username(params, update, context)

    return None


def check_dev_access(user_id: int) -> bool:
    return user_id in _dev_ids


def is_banned(user_id: int) -> bool:
    return user_id in _banned_users
