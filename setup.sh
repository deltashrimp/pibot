#!/bin/bash
echo "Запуск установки Пибота"
echo "Нажмите ctrl+C для отмены"
echo -e "------------------------------------------------\n"

sleep 3

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || exit 1

echo -e "Копирование файлов конфигурации...\n"

cp ./bot-data/public-phrases.json ./bot-data/phrases.json
cp ./bot-data/public-botinfo.md ./bot-data/botinfo.md

mkdir -pv logs && touch logs/logs.log

echo -e "Создание файлов с ключами...\n"

mkdir -pv ./env/
echo "[]" > ./env/dev-ids.json

cp .env.example .env

echo -e "------------------------------------------------\n"
echo -e "[WARNING]: Вставьте токен бота в .env (TELEGRAM_TOKEN) (получить у BotFather)\n"
echo -e "Вы можете настроить свои фразы в bot-data/phrases.json\n"
echo -e "Отредактируйте bot-data/botinfo.md чтобы изменить сообщение о боте\n"
echo -e "------------------------------------------------\n\n"

sleep 3

chmod +x ./launchbot.sh

echo -e "Установка зависимостей...\n"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -r requirements.txt

echo -e "------------------------------------------------\n"
echo -e "Установка завершена! Запустите бота: ./launchbot.sh\n"
echo -e "------------------------------------------------"
