import logging

from aiogram.methods.base import TelegramMethod
from aiogram.types import ChatIdUnion, ChatMemberUpdated, Message

from persistence import SQLitePersistence

logger = logging.getLogger(__name__)

RAID_PROTECTION_KEY = "raid_protection"


class SetChatSlowModeDelay(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "setChatSlowModeDelay"

    chat_id: ChatIdUnion
    slow_mode_delay: int


async def get_protection_state(persistence: SQLitePersistence, chat_id: int) -> bool:
    return await persistence.get_chat_config(chat_id, RAID_PROTECTION_KEY, "0") == "1"


async def set_protection_state(
    persistence: SQLitePersistence, chat_id: int, enabled: bool
) -> None:
    await persistence.set_chat_config(
        chat_id, RAID_PROTECTION_KEY, "1" if enabled else "0"
    )


async def handle_raid_protection(
    message: Message,
    chat_data: dict,
    bot_data: dict,
    params: str,
) -> None:
    persistence: SQLitePersistence | None = bot_data.get("persistence")
    if persistence is None:
        logger.error("[AntiRaid] persistence not found in bot_data")
        return

    chat_id = message.chat.id
    bot = message.bot
    assert bot is not None
    enabled = await get_protection_state(persistence, chat_id)

    if enabled:
        await set_protection_state(persistence, chat_id, False)
        try:
            await bot(SetChatSlowModeDelay(chat_id=chat_id, slow_mode_delay=0))
        except Exception as e:
            logger.warning("[AntiRaid] Failed to reset slow mode: %s", e)
        await message.answer("Защита выключена")
    else:
        await set_protection_state(persistence, chat_id, True)
        try:
            await bot(SetChatSlowModeDelay(chat_id=chat_id, slow_mode_delay=300))
        except Exception as e:
            logger.warning("[AntiRaid] Failed to set slow mode: %s", e)
        await message.answer("Чат под защитой")


async def on_chat_member(
    update: ChatMemberUpdated,
    persistence: SQLitePersistence,
) -> None:
    chat_id = update.chat.id

    if not await get_protection_state(persistence, chat_id):
        return

    old_status = update.old_chat_member.status
    new_status = update.new_chat_member.status

    was_not_member = old_status in ("left", "kicked")
    is_now_member = new_status in ("member", "restricted")

    if was_not_member and is_now_member:
        user = update.new_chat_member.user
        if user and not user.is_bot:
            bot = update.bot
            assert bot is not None
            try:
                await bot.ban_chat_member(chat_id, user.id)
                await bot.unban_chat_member(chat_id, user.id)
                logger.info(
                    "[AntiRaid] Kicked %d from %d (raid protection)",
                    user.id,
                    chat_id,
                )
            except Exception as e:
                logger.warning("[AntiRaid] Failed to kick %d: %s", user.id, e)
