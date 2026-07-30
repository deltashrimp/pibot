from aiogram.types import Message

GREETER_TEXT = (
    "Привет. Я Пибот - лучший модератор чатов. \n "
    "Только ты прежде чем меня в чаты добавлять, проверь какие данные я собираю. \n"
    "teletype.in/@pibot_news/meet_the_pibot#nvGL"
)


async def cmd_start(message: Message) -> None:
    await message.answer(GREETER_TEXT)
