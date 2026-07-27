# Pibot — Telegram Chat Bot

Многофункциональный Telegram-бот с фразовыми ответами, RP-командами, AI-интеграцией (Groq Llama) и модерацией.

## Возможности

- **Фразовые ответы** — автоматически отвечает на триггер-фразы из `phrases.json`
- **RP-команды** — интерактивный ролеплей (обнять, поцеловать и т.д.) через ответ на сообщение
- **AI-интеграция** — отвечает с контекстом при @упоминании бота (Groq Llama 3.3 70B через OpenAI SDK)
- **Админ-команды** — `сотри`, `мут`, `размут`
- **Суперюзер-команды** — `кикни`, `кинь`, `заблокируй`
- **Антиспам** — rate limiter, age filter, защита от спама триггер-фразами, Telegram-мут
- **Ранговая система** — 4 уровня доступа (Owner, Admin+, Admin, Member)

## Структура

```
pibot/
├── bot-data/         # JSON/MD файлы данных (phrases, rp-commands, personality)
├── env/              # ID разработчиков (gitignored)
├── info/             # Документация и справка
├── important/        # Настройка логирования и утилиты (gitignored)
├── source/
│   ├── pibot.py      # Основной код бота (класс, ~1150 строк)
│   └── persistence.py # SQLite persistence
├── .env.example      # Шаблон переменных окружения
├── Dockerfile        # Контейнеризация
├── docker-compose.yml # Docker Compose
├── pibot.service     # systemd unit
├── setup.sh          # Скрипт развёртывания
└── launchbot.sh      # Скрипт запуска
```

## Установка

1. Скопировать репозиторий на сервер
2. Запустить `bash setup.sh` — создаст файлы конфигов, venv и установит зависимости
3. Вставить токен бота и API ключи в `.env` (TELEGRAM_TOKEN, GROQ_KEY)
4. Опционально настроить `bot-data/personality.md` и `bot-data/botinfo.md`
5. Запустить `./launchbot.sh`

### Docker

```bash
docker compose up -d
```

### systemd

```bash
sudo cp pibot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pibot
sudo systemctl start pibot
# Логи: journalctl -u pibot -f
```

## Команды

Полный список в `info/command-list.md` или по фразе `пибот команды` в чате.

## Зависимости

- python-telegram-bot (v22.7, с job-queue)
- openai (v1.70.0, AsyncOpenAI для Groq API)
- python-dotenv (v1.0.1)
