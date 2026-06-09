import logging
import os
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

from llm_service import init_clients, personality_prompt
from message_handlers import handle_message, safe_reply, track_all_messages
from storage import TOKEN_PATH, PERSONALITY_PATH, WELCOME_MESSAGE, load_data
import moderation  # noqa: F401 — triggers @pibot_command decoration

logger = logging.getLogger(__name__)


async def start(update: Update, context: CallbackContext) -> None:
    await safe_reply(update, context, WELCOME_MESSAGE)


async def startup_notify(context: CallbackContext) -> None:
    known = context.bot_data.get("known_chats", set())
    for chat_id in known:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=WELCOME_MESSAGE,
            )
        except Exception:
            pass


async def post_init(app: Application) -> None:
    if not app.bot_data.get("startup_notified"):
        app.job_queue.run_once(startup_notify, when=3)
        app.bot_data["startup_notified"] = True
    for chat_data in app.chat_data.values():
        chat_data.pop("llm_history", None)
    init_clients()


def main() -> None:
    global personality_prompt

    token = os.getenv("PIBOT_TOKEN") or TOKEN_PATH.read_text().strip()
    if not token or token == "YOUR-TELEGRAM-TOKEN":
        print("Пожалуйста вставьте токен бота в telegram-token")
        return

    if PERSONALITY_PATH.exists():
        personality_prompt = PERSONALITY_PATH.read_text().strip()
    else:
        print("personality.md не найден. ИИ выключен")

    load_data()

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
