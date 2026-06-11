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
touch ./bot-data/banned-users.json

cat > ./bot-data/personality.md << 'EOF'
# PiBot personality

Ты — дружелюбный и остроумный помощник в Telegram-чате.
Отвечай кратко, с юмором, на русском языке.
Твоя задача — поддерживать беседу и помогать участникам.
EOF

echo -e "Создание файлов с ключами...\n"

mkdir -p ./env/
echo "[]" > ./env/dev-ids.json

cp .env.example .env

echo -e "------------------------------------------------\n"
echo -e "[WARNING]: Вставьте токен бота в .env (TELEGRAM_TOKEN) (получить у BotFather)\n"
echo -e "[WARNING]: Вставьте API ключ в .env (GROQ_KEY)\n"
echo -e "Вы можете настроить свои фразы в bot-data/phrases.json\n"
echo -e "Отредактируйте bot-data/botinfo.md чтобы изменить сообщение о боте\n"
echo -e "Отредактируйте bot-data/personality.md чтобы изменить поведение ИИ\n"
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
