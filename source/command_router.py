"""Command routing and moderation/admin command handlers."""

import asyncio
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

from aiogram.enums import ChatMemberStatus
from aiogram.types import (
    CallbackQuery,
    Chat,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
)

from chat_service import get_user_rank, resolve_user, target_immune_to_mkb
from constants import (
    DELETE_BATCH_SIZE,
    MAX_COMMAND_ARG_LENGTH,
    MAX_TRACKED_MESSAGES,
    MAX_WRITE_TEXT_LENGTH,
    PIBOT_PREFIX,
    PIBOT_PREFIX_LEN,
    RANK_ADMIN,
    RANK_ADMIN_PLUS,
    RANK_MEMBER,
    RANK_OWNER,
)
from persistence import ChatData
from utils import ALL_PERMISSIONS, NO_PERMISSIONS, get_mention, pluralize_minutes

if TYPE_CHECKING:
    from pibot import PiBot

logger = logging.getLogger(__name__)

MAX_NUKE_MESSAGES = 1000
MAX_MUTE_MINUTES = 365 * 24 * 60  # 365 days
GIT_CLONE_DIR_SIZE_LIMIT = 100 * 1024 * 1024  # 100 MB
GIT_ZIP_SIZE_LIMIT = 40 * 1024 * 1024  # 40 MB
ALLOWED_GIT_SCHEMES = ("http", "https", "ssh", "git")


@dataclass
class CommandConfig:
    """Maps a command name to its handler and minimum required rank."""

    handler: Callable[..., Any]
    value: int


def _validate_git_url(url: str) -> bool:
    """Validate a git clone URL to prevent command/option injection."""
    if not url or url.startswith("-"):
        return False
    if any(char in url for char in (";", "|", "&", "\n", "\r")):
        return False
    if "--" in url:
        return False
    parsed = urlparse(url)
    if parsed.scheme in ALLOWED_GIT_SCHEMES:
        return bool(parsed.netloc)
    if parsed.scheme == "":
        # scp-style URL: user@host:path/to/repo.git
        return bool(re.match(r"^[A-Za-z0-9_.+-]+@[A-Za-z0-9_.-]+:.+", url))
    return False


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


class CommandRouter:
    """Maps command names to handlers and dispatches them."""

    def __init__(self, pibot: "PiBot") -> None:
        self.pibot = pibot
        self.commands: dict[str, CommandConfig] = {}
        self._register_commands()

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
        self.commands["защита"] = CommandConfig(self.pibot.raid_handler, 2)

    async def _reject_long_params(self, message: Message, params: str) -> bool:
        """Reject oversized command arguments; returns True if rejected."""
        if len(params) > MAX_COMMAND_ARG_LENGTH:
            await self.pibot.safe_reply(
                message,
                "⚠️ Аргумент слишком длинный",
            )
            return True
        return False

    async def handle_command(
        self, message: Message, chat_data: ChatData, bot_data: dict, text: str
    ) -> bool:
        """Parse and dispatch a command; returns True if it was handled."""
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
        user = message.from_user
        assert user is not None
        user_id = user.id

        if cmd.value == 0:
            if user_id not in self.pibot.dev_ids:
                await self.pibot.safe_reply(
                    message, "⛔️ Недостаточно прав для этой команды"
                )
                return True
        else:
            user_rank = await get_user_rank(message.chat, chat_data, user_id)
            if user_rank > cmd.value:
                await self.pibot.safe_reply(
                    message, "⛔️ Недостаточно прав для этой команды"
                )
                return True

        await cmd.handler(message, chat_data, bot_data, params)
        return True

    async def handle_nuke(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        if not params:
            await self.pibot.safe_reply(
                message,
                "Использование: пибот сотри n, где n - целое положительное число",
            )
            return
        if await self._reject_long_params(message, params):
            return

        try:
            n = int(params)
            if n <= 0 or n > MAX_NUKE_MESSAGES:
                raise ValueError
        except ValueError:
            await self.pibot.safe_reply(
                message,
                f"Использование: пибот сотри n, где n - целое число от 1 до {MAX_NUKE_MESSAGES}",
            )
            return

        bot = message.bot
        assert bot is not None
        chat_id = message.chat.id
        async with self.pibot._get_msg_lock(chat_id):
            user_ids = chat_data.message_ids
            bot_ids = self.pibot.bot_message_ids.setdefault(chat_id, deque())

            all_ids = set(user_ids) | set(bot_ids)
            if not all_ids:
                await self.pibot.safe_reply(message, "⚠️ Не найдено сообщений")
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
            await self.pibot.safe_reply(message, "⚠️ Нет сообщений для удаления")
            return

        total = len(ids_to_delete)
        deleted = 0
        for i in range(0, total, DELETE_BATCH_SIZE):
            batch = ids_to_delete[i : i + DELETE_BATCH_SIZE]
            try:
                await bot.delete_messages(chat_id=chat_id, message_ids=batch)
                deleted += len(batch)
            except Exception as e:
                logger.warning(
                    "[Nuke] не удалось удалить %d сообщений: %s",
                    len(batch),
                    e,
                )

        if deleted == 0:
            await self.pibot.safe_reply(message, "⚠️ Не удалось удалить сообщения")

    async def handle_kick(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        if await self._reject_long_params(message, params):
            return
        target = await resolve_user(message, bot_data, message.chat, params)
        if not target:
            await self.pibot.safe_reply(
                message,
                "⚠️ Кого выкинуть? Ответь на сообщение или укажи @username",
            )
            return

        if await target_immune_to_mkb(message.chat, chat_data, target.id):
            await self.pibot.safe_reply(message, "⛔️ Этого пользователя нельзя выкинуть")
            return

        bot = message.bot
        assert bot is not None
        try:
            await bot.ban_chat_member(message.chat.id, target.id)
            await bot.unban_chat_member(message.chat.id, target.id)
            await self.pibot.safe_reply(
                message, f"✅️ {get_mention(target)} выкинут за борт"
            )
        except Exception as e:
            await self.pibot.safe_reply(message, f"⚠️ Ошибка кика: {e}")

    async def handle_ban(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        if await self._reject_long_params(message, params):
            return
        target = await resolve_user(message, bot_data, message.chat, params)
        if not target:
            await self.pibot.safe_reply(
                message,
                "⚠️ Цель не найдена. Ответь на сообщение или укажи @username",
            )
            return

        if await target_immune_to_mkb(message.chat, chat_data, target.id):
            await self.pibot.safe_reply(
                message, "⛔️ Этого пользователя нельзя ликвидировать"
            )
            return

        bot = message.bot
        assert bot is not None
        try:
            await bot.ban_chat_member(
                message.chat.id, target.id, revoke_messages=True
            )
            await self.pibot.persistence.add_global_ban(target.id)
            await self.pibot.safe_reply(
                message, f"✅️ {get_mention(target)} был ликвидирован"
            )
        except Exception as e:
            await self.pibot.safe_reply(message, f"⚠️ Ошибка ликвидации: {e}")

    async def handle_unban(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        if await self._reject_long_params(message, params):
            return
        target = await resolve_user(message, bot_data, message.chat, params)
        if not target:
            await self.pibot.safe_reply(
                message,
                "⚠️ Кого воскресить? Ответь на сообщение или укажи @username",
            )
            return

        bot = message.bot
        assert bot is not None
        try:
            await bot.unban_chat_member(
                message.chat.id, target.id, only_if_banned=True
            )
            await self.pibot.persistence.remove_global_ban(target.id)
            await self.pibot.safe_reply(message, f"✅️ {get_mention(target)} воскрес")
        except Exception as e:
            await self.pibot.safe_reply(message, f"⚠️ Ошибка воскрешения: {e}")

    async def handle_block(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        if not params:
            await self.pibot.safe_reply(
                message,
                "Использование: пибот заблокируй <id> или @username",
            )
            return
        if await self._reject_long_params(message, params):
            return

        target = await resolve_user(message, bot_data, message.chat, params)
        if target:
            target_id = target.id
        else:
            try:
                target_id = int(params.strip())
            except ValueError:
                await self.pibot.safe_reply(
                    message, "⚠️ Укажи числовой ID или @username"
                )
                return

        await self.pibot.persistence.add_global_ban(target_id)
        await self.pibot.safe_reply(
            message, f"✅️ Пользователь {target_id} заблокирован"
        )

    async def handle_unblock(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        if not params:
            await self.pibot.safe_reply(
                message,
                "Использование: пибот разблокируй <id> или @username",
            )
            return
        if await self._reject_long_params(message, params):
            return

        target = await resolve_user(message, bot_data, message.chat, params)
        if target:
            target_id = target.id
        else:
            try:
                target_id = int(params.strip())
            except ValueError:
                await self.pibot.safe_reply(
                    message, "⚠️ Укажи числовой ID или @username"
                )
                return

        if target_id not in self.pibot.persistence.banned_users:
            await self.pibot.safe_reply(
                message, f"⚠️ Пользователь {target_id} не в чёрном списке"
            )
            return

        await self.pibot.persistence.remove_global_ban(target_id)
        await self.pibot.safe_reply(
            message, f"✅️ Пользователь {target_id} разблокирован"
        )

    async def handle_mute(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        if await self._reject_long_params(message, params):
            return
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

        if duration_minutes is not None and duration_minutes > MAX_MUTE_MINUTES:
            await self.pibot.safe_reply(
                message, "⚠️ Максимальный срок мута — 365 дней"
            )
            return

        target = await resolve_user(message, bot_data, message.chat, user_params)
        if not target:
            await self.pibot.safe_reply(
                message,
                "⚠️ Кого мутить? Ответь на сообщение или укажи @username",
            )
            return

        if await target_immune_to_mkb(message.chat, chat_data, target.id):
            await self.pibot.safe_reply(
                message, "⛔️ Этого пользователя нельзя замутить"
            )
            return

        bot = message.bot
        assert bot is not None
        try:
            if duration_minutes is not None:
                until_date = int(time.time()) + int(duration_minutes * 60)
                await bot.restrict_chat_member(
                    message.chat.id,
                    target.id,
                    permissions=NO_PERMISSIONS,
                    until_date=until_date,
                )
                await self.pibot.safe_reply(
                    message,
                    f"✅️ {get_mention(target)} рот прикрой на {duration_minutes} "
                    f"{pluralize_minutes(int(duration_minutes))}",
                )
            else:
                await bot.restrict_chat_member(
                    message.chat.id,
                    target.id,
                    permissions=NO_PERMISSIONS,
                )
                await self.pibot.safe_reply(
                    message, f"✅️ {get_mention(target)} не глаголь тут"
                )
        except Exception as e:
            await self.pibot.safe_reply(message, f"⚠️ Ошибка мута: {e}")

    async def handle_unmute(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        if await self._reject_long_params(message, params):
            return
        target = await resolve_user(message, bot_data, message.chat, params)
        if not target:
            await self.pibot.safe_reply(
                message,
                "⚠️ Кого размутить? Ответь на сообщение или укажи @username",
            )
            return

        bot = message.bot
        assert bot is not None
        try:
            await bot.restrict_chat_member(
                message.chat.id,
                target.id,
                permissions=ALL_PERMISSIONS,
            )
            await self.pibot.safe_reply(
                message, f"✅️ {get_mention(target)} больше не буянь"
            )
        except Exception as e:
            await self.pibot.safe_reply(message, f"⚠️ Ошибка размута: {e}")

    async def handle_rank(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        if await self._reject_long_params(message, params):
            return
        parts = params.split(maxsplit=2)
        if len(parts) < 1:
            await self.pibot.safe_reply(
                message,
                "Использование: пибот ранг n для @user (n = 2, 3, 4)",
            )
            return

        try:
            new_rank = int(parts[0])
        except ValueError:
            await self.pibot.safe_reply(
                message,
                "Использование: пибот ранг n для @user (n = 2, 3, 4)",
            )
            return

        if new_rank not in (RANK_ADMIN_PLUS, RANK_ADMIN, RANK_MEMBER):
            await self.pibot.safe_reply(message, "Ранг может быть только 2, 3 или 4")
            return

        target_str = ""
        if len(parts) >= 3 and parts[1] == "для":
            target_str = parts[2]
        elif len(parts) == 2:
            target_str = parts[1]

        target = await resolve_user(message, bot_data, message.chat, target_str)
        if not target:
            await self.pibot.safe_reply(
                message,
                "⚠️ Кому изменить ранг? Ответь на сообщение или укажи @username",
            )
            return

        bot_user = await self.pibot.bot.me()
        if target.id == bot_user.id:
            await self.pibot.safe_reply(message, "⛔️ Нельзя изменить ранг бота")
            return

        target_rank = await get_user_rank(message.chat, chat_data, target.id)
        if target_rank == RANK_OWNER:
            await self.pibot.safe_reply(message, "⛔️ Нельзя изменить ранг владельца")
            return

        try:
            target_member = await message.chat.get_member(target.id)
            is_tg_admin = target_member.status == ChatMemberStatus.ADMINISTRATOR
        except Exception:
            is_tg_admin = False

        if is_tg_admin and new_rank == RANK_MEMBER:
            await self.pibot.safe_reply(
                message,
                "⛔️ Нельзя выдать ранг 4 администратору. Понизьте его через Telegram.",
            )
            return

        if not is_tg_admin and new_rank in (RANK_ADMIN_PLUS, RANK_ADMIN):
            await self.pibot.safe_reply(
                message,
                "⛔️ Нельзя выдать ранг 2 или 3 обычному участнику. "
                "Сначала выдайте админку через Telegram.",
            )
            return

        chat_data.ranks[target.id] = new_rank
        await self.pibot.persistence.set_chat_rank(
            message.chat.id, target.id, new_rank
        )

        rank_names = {
            RANK_ADMIN_PLUS: "Админ+",
            RANK_ADMIN: "Админ",
            RANK_MEMBER: "Участник",
        }
        await self.pibot.safe_reply(
            message,
            f"✅️ Ранг {get_mention(target)} изменён на {rank_names[new_rank]}",
        )

    async def handle_botinfo_cmd(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        link = self.pibot.links.get("био")
        if link:
            await self.pibot.safe_reply(message, link)
        else:
            await self.pibot.safe_reply(message, "⚠️ Ссылка не настроена")

    async def handle_changelog_cmd(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        link = self.pibot.links.get("обновы")
        if link:
            await self.pibot.safe_reply(message, link)
        else:
            await self.pibot.safe_reply(message, "⚠️ Ссылка не настроена")

    async def handle_commands_cmd(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        link = self.pibot.links.get("команды")
        if link:
            await self.pibot.safe_reply(message, link)
        else:
            await self.pibot.safe_reply(message, "⚠️ Ссылка не настроена")

    async def handle_devcommands_cmd(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        link = self.pibot.links.get("дев команды")
        if link:
            await self.pibot.safe_reply(message, link)
        else:
            await self.pibot.safe_reply(message, "⚠️ Ссылка не настроена")

    async def handle_rank_list(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        ranks = chat_data.ranks
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
            await self.pibot.safe_reply(message, "Нет пользователей с особыми рангами")
            return

        await self.pibot.safe_reply(message, "\n".join(lines))

    async def handle_clear_db(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        try:
            await self.pibot.persistence.clear_all()
            await self.pibot.safe_reply(message, "✅ База данных очищена")
        except Exception as e:
            logger.error("[clear_db] %s", e, exc_info=True)
            await self.pibot.safe_reply(
                message, f"⚠️ Ошибка очистки базы данных: {e}"
            )

    async def handle_git_clone(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        url = params.strip()
        if not url:
            await self.pibot.safe_reply(
                message,
                "Использование: пибот клонируй <url>",
            )
            return

        if not _validate_git_url(url):
            await self.pibot.safe_reply(
                message,
                "⚠️ Некорректный URL. Укажи ссылку вида https://github.com/...",
            )
            return

        status_msg = await self.pibot.safe_reply(message, "Ща, жди")

        tmp_dir = None
        zip_path = None
        try:
            tmp_dir = tempfile.mkdtemp(prefix="pibot_clone_")

            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--",
                url,
                tmp_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_text = (
                    stderr.decode().strip() if stderr else "неизвестная ошибка"
                )
                await self.pibot.safe_reply(
                    message, f"⚠️ Ошибка клонирования:\n{error_text[:500]}"
                )
                return

            dir_size = _dir_size(tmp_dir)
            if dir_size > GIT_CLONE_DIR_SIZE_LIMIT:
                await self.pibot.safe_reply(
                    message,
                    f"⚠️ Репозиторий слишком большой "
                    f"({dir_size / 1024 / 1024:.1f} MB). Лимит 100 MB.",
                )
                return

            repo_name = url.rstrip("/").split("/")[-1].removesuffix(".git") or "repo"
            zip_path = f"/tmp/{repo_name}.zip"

            try:
                if status_msg:
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
                await self.pibot.safe_reply(
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
            await self.pibot.safe_reply(message, f"⚠️ Ошибка: {e}")
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            if zip_path and os.path.exists(zip_path):
                os.remove(zip_path)

    async def handle_chance_cmd(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        n = random.randint(0, 100)
        await self.pibot.safe_reply(
            message,
            f"Я думаю, что вероятность {n}%",
            disable_notification=True,
        )

    async def handle_all_chats(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        known = self.pibot.persistence.known_chats
        if not known:
            await self.pibot.safe_reply(message, "Нет известных чатов")
            return

        bot = message.bot
        assert bot is not None
        lines: list[str] = []
        for cid in sorted(known)[:50]:
            try:
                chat = await bot.get_chat(cid)
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
        await self.pibot.safe_reply(message, f"📋 Все чаты ({len(known)}):\n\n{reply}")

    async def handle_ai_cmd(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        current = bot_data.get("llm_provider", "")
        user = message.from_user
        assert user is not None
        buttons = []
        for p in self.pibot.ai_service.providers.values():
            if p.enabled:
                label = f"{'✅ ' if p.name == current else ''}{p.display_name}"
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=label,
                            callback_data=f"aichange:{user.id}:{p.name}",
                        )
                    ]
                )

        if not buttons:
            await self.pibot.safe_reply(message, "⚠️ Нет доступных AI провайдеров")
            return

        current_name = (
            self.pibot.ai_service.providers[current].display_name
            if current in self.pibot.ai_service.providers
            else "—"
        )
        await self.pibot.safe_reply(
            message,
            f"🎛 Текущий AI провайдер: {current_name}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    async def handle_ai_change(self, callback: CallbackQuery) -> None:
        if not callback.data:
            return
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
            provider_name not in self.pibot.ai_service.providers
            or not self.pibot.ai_service.providers[provider_name].enabled
        ):
            await callback.answer("⚠️ Провайдер недоступен", show_alert=True)
            return

        await self.pibot.persistence.set_bot_config("llm_provider", provider_name)

        msg = callback.message
        if isinstance(msg, Message):
            await msg.edit_text(
                f"✅ AI провайдер переключён на "
                f"{self.pibot.ai_service.providers[provider_name].display_name}"
            )
        await callback.answer()

    async def handle_write_cmd(
        self,
        message: Message,
        chat_data: ChatData,
        bot_data: dict,
        params: str,
    ) -> None:
        if not params:
            await self.pibot.safe_reply(
                message, "Использование: пибот напиши <текст>"
            )
            return
        if len(params) > MAX_WRITE_TEXT_LENGTH:
            await self.pibot.safe_reply(
                message,
                f"⚠️ Текст слишком длинный (максимум {MAX_WRITE_TEXT_LENGTH} символов)",
            )
            return

        known = self.pibot.persistence.known_chats
        if not known:
            await self.pibot.safe_reply(message, "Нет известных чатов")
            return

        user = message.from_user
        assert user is not None
        bot = message.bot
        assert bot is not None
        self.pibot.pending_writes[user.id] = params

        buttons: list[list[InlineKeyboardButton]] = []
        for cid in sorted(known)[:50]:
            try:
                chat = await bot.get_chat(cid)
                title = chat.title or chat.username or chat.first_name or str(cid)
            except Exception:
                title = str(cid)
            buttons.append(
                [
                    InlineKeyboardButton(
                        text=title,
                        callback_data=f"writechat:{user.id}:{cid}",
                    )
                ]
            )

        await self.pibot.safe_reply(
            message,
            "📨 В какой чат отправить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )

    async def handle_write_chat(self, callback: CallbackQuery) -> None:
        if not callback.data:
            return
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

        text = self.pibot.pending_writes.pop(caller_id, None)
        if text is None:
            await callback.answer("⚠️ Текст устарел, попробуй снова", show_alert=True)
            return

        try:
            bot = callback.bot
            assert bot is not None
            await bot.send_message(target_chat_id, text)
            msg = callback.message
            if isinstance(msg, Message):
                await msg.edit_text(f"✅ Отправлено в чат {target_chat_id}")
            await callback.answer()
        except Exception as e:
            await callback.answer(f"❌ Не удалось отправить: {e}", show_alert=True)
