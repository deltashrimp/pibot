# Архитектура и функциональность Pibot

## Структура проекта

```
pibot/
├── bot-data/                   # Данные, загружаемые при старте
│   ├── phrases.json            # Триггер-фразы и ответы
│   ├── rp-phrases.json         # RP-команды (ответом на сообщение)
│   ├── personality.md          # Системный промпт для LLM
│   ├── botinfo.md              # Вывод по "пибот био"
│   ├── changelog.md            # Вывод по "пибот обновы"
│   ├── dev-commands.md         # Список dev-only команд
│   ├── public-phrases.json     # Шаблон phrases.json
│   └── public-botinfo.md       # Шаблон botinfo.md
├── env/                        # Неотслеживаемые конфиги (gitignored)
│   ├── dev-ids.json            # ID разработчиков (int[])
│   ├── groq-key                # Groq API key (запасной, не используется)
│   ├── openrouter-key          # OpenRouter API key (запасной, не используется)
│   └── telegram-token          # Telegram токен (запасной, не используется)
├── info/                       # Документация и справка
│   ├── command-list.md         # Список команд (вывод по "пибот команды")
│   ├── documentation.md        # Эта документация
│   ├── documentation.json      # Документация для LLM
│   ├── full-changelog.md       # Полный список изменений
│   └── README-*.md             # README на разных языках
├── source/
│   ├── pibot.py                # Основной код бота (класс PiBot, ~1490 строк)
│   ├── persistence.py          # SQLite persistence (standalone, 137 строк)
│   └── logging_settings.py     # Цветное логирование в консоль
├── .env                        # TELEGRAM_TOKEN, GROQ_KEY, OPENROUTER_KEY (gitignored)
├── .env.example                # Шаблон переменных окружения
├── Dockerfile                  # Контейнеризация
├── docker-compose.yml          # Docker Compose
├── pibot.service               # systemd unit
├── pyproject.toml              # Конфигурация проекта и mypy
├── setup.sh                    # Скрипт развёртывания
├── launchbot.sh                # Скрипт запуска
├── requirements.txt            # Зависимости (точные версии)
└── README.md                   # Главный README
```

## Персистентность

Данные бота хранятся в SQLite (`source/bot_data.db`). Реализация — собственный класс `SQLitePersistence` в `source/persistence.py`.

Данные чатов (`chat_data`), данные бота (`bot_data`), данные пользователей (`user_data`) сериализуются через JSON с поддержкой `set` и `deque` (через `SetEncoder` + `_object_hook`). Загрузка/сохранение через `asyncio.to_thread`.

## Класс PiBot

Весь функционал инкапсулирован в класс `PiBot` (`source/pibot.py:405`). При инициализации:

1. Загружает `phrases.json`, `rp-phrases.json`, `dev-ids.json` в память
2. Загружает `personality.md` как системный промпт (общий для всех AI-провайдеров)
3. Инициализирует AI-провайдеров (Groq, OpenRouter) через `_init_providers()`
4. Регистрирует команды через `_register_commands()`
5. Создаёт `Bot` и `Dispatcher` из aiogram, регистрирует `PiBotMiddleware`

### Aiogram-специфика

- `Bot` создаётся с `DefaultBotProperties(parse_mode=ParseMode.HTML)`
- `Dispatcher` — единый, без роутеров
- middleware: `PiBotMiddleware` на все входящие сообщения (`self.dp.message.middleware(...)`)
- callback query handler: `self.dp.callback_query(lambda c: c.data and c.data.startswith("aichange:"))`
- `self.dp.message()` — единый handler на все сообщения

## Обработка сообщений (`PiBot.handle_message`)

Сообщение сначала проходит через `PiBotMiddleware`, который:
- Инициализирует `chat_data` / `bot_data` для чата
- Трекает `message_ids` в deque
- Добавляет чат в `known_chats`
- Обновляет `username_map`
- В группах: добавляет сообщение в `chat_history` (последние `AI_MAX_HISTORY=20` сообщений)
- Антиспам: progressive warning → mute для не-админов

Затем `handle_message`:

1. **Pre-check** (`_pre_check()`) — блокировка забаненных, фильтр возраста (>120 сек, команды с `пибот` пропускаются)
2. **Команды** (`_handle_command()`) — `пибот <команда>`, проверка dev_only или ранга
3. **Игнор** (`is_user_ignored()`) — таймаут триггер-спама
4. **RP-команды** (`_handle_rp()`) — ответ на сообщение, совпадение с RP-фразой
5. **Триггер-фразы** (`_handle_phrase()`) — точное совпадение в `phrases.json`
6. **AI-ответ** (`_handle_ai()`) — @упоминание бота → вызов текущего AI-провайдера

## Система команд

Команды вызываются через префикс `пибот <команда>`.
Регистрируются методом `PiBot._register_commands()`, хранятся в `self.commands` как `CommandConfig(handler, value, dev_only)`.

### Ранговая система

4 уровня доступа:

| Ранг | Название | Кто | Неприкосновенность |
|------|----------|-----|--------------------|
| 1 | Owner | Создатель чата (Telegram owner) | mute, kick, ban, rank change |
| 2 | Admin+ | Назначается владельцем | mute, kick, ban |
| 3 | Admin | Админы чата по умолчанию | mute, kick, ban |
| 4 | Member | Все остальные | — |

- `user_rank <= command.value` даёт доступ
- Назначить можно только ранг ниже своего
- Нельзя назначить ранг 4 Telegram-админу
- Нельзя назначить ранг 2 или 3 не-админу

### Команды и их ранги

| Команда | Value | Ранг | Описание |
|---------|-------|------|----------|
| `кинь в гулаг @user` | 1 | Owner | Telegram-бан + запись в bot_data |
| `верни @user` | 1 | Owner | Telegram-анбан + удаление из bot_data |
| `ранг n для @user` | 1 | Owner | Изменить ранг (n = 2, 3, 4) |
| `сотри n` | 2 | Admin+ | Удалить n+1 последних сообщений |
| `кикни @user` | 2 | Admin+ | Кик пользователя |
| `мут @user [n]` | 3 | Admin | Мут на n минут (мин. 0.5) |
| `размут @user` | 3 | Admin | Размут |
| `био` | 4 | Member | Информация о боте |
| `обновы` | 4 | Member | Список изменений |
| `команды` | 4 | Member | Список команд |
| `ранги` | 4 | Member | Пользователи с особыми рангами |
| `инфа` | 4 | Member | Случайное число 0-100 |

### Dev-only команды (только из `env/dev-ids.json`)

| Команда | Описание |
|---------|----------|
| `ии` | Выбрать AI-провайдера через inline-кнопки |
| `заблокируй <id> или @user` | Глобальный бан в bot_data (без Telegram) |
| `разблокируй <id> или @user` | Снять глобальный бан |
| `все чаты` | Список всех известных чатов |
| `очистка бд` | Очистить базу данных |

### Hard block (`self.banned_users`)

Хранится в памяти (`self.banned_users`, set[int]), инициализируется из `bot_data['banned_users']`, синхронизируется при изменениях:
- `кинь в гулаг` — добавляет ID + Telegram-ban с revoke_messages=True
- `верни` — удаляет ID + Telegram-unban с only_if_banned=True
- `заблокируй` — добавляет ID без Telegram-бана (dev_only)
- `разблокируй` — удаляет ID (dev_only)

## AI-система

### Провайдеры

Абстракция `AIBackend` (`source/pibot.py:141`) — класс-обёртка для OpenAI-совместимых API:

- `name` — внутреннее имя (groq, openrouter)
- `display_name` — человеческое имя (Groq, OpenRouter)
- `client` — API-клиент (`AsyncGroq` / `AsyncOpenAI`) или None
- `model` — модель по умолчанию
- `enabled` — True если клиент создан
- `generate(messages) → str` — единый метод вызова

Текущий провайдер хранится в `bot_data["llm_provider"]` и переключается командой `пибот ии`.

### Конфигурация

- Ключи: `GROQ_KEY`, `OPENROUTER_KEY` из `.env`
- Системный промпт: `bot-data/personality.md`
- Общий rate limit: 2 вызова / 60 секунд (`self.ai_limiter`)
- Модели:
  - Groq: `llama-3.3-70b-versatile`
  - OpenRouter: `google/gemma-4-31b-it:free`
- `max_tokens`: 512, `temperature`: 0.7

### Контекст

- Личные сообщения: `conversation_history[user_id]` — история диалога (in-memory)
- Группы: `chat_history[chat_id]` — последние 20 сообщений от всех пользователей, формат `@user said: text` (добавляется в `PiBotMiddleware`)
- Системный промпт + контекст → `messages` для API

### Вызов

1. Проверка `@упоминания` в группах
2. Acquire `self.ai_limiter` (2/60s)
3. `_build_ai_messages()` — формирование списка сообщений
4. `provider.generate(messages)` — API-вызов
5. Обработка `RateLimitError` (429) → сообщение пользователю
6. Остальные ошибки → "⚠️ Ошибка при обращении к ИИ"
7. В ЛС: сохранение истории (user + assistant)

## Антиспам

### PiBotMiddleware — трекинг и антиспам

Middleware в `PiBot` выполняет:
- **Трекинг**: `message_ids` в deque (до 1000), `known_chats`, `username_map`
- **Chat history**: последние 20 сообщений для AI-контекста в группах
- **Антиспам** (только для групп, не для админов):
  - Sliding window 1 секунда, лимит 5 сообщений
  - 6-9: предупреждение "пожалуйста, не флуди" (1 раз через spam_warned)
  - >9: `restrictChatMember` на 60 секунд

### Trigger phrase spam filter

- >5 одинаковых триггер-фраз за 10 секунд: ответ "Ой всё", игнор на 60 секунд

### Age filter

- Сообщения старше 120 секунд игнорируются (кроме команд с `пибот`)
- Защита от обработки старых сообщений при перезапуске

### Очистка кэша (`_cleanup_caches`)

Раз в час: удаление мёртвых чатов из `msg_locks`, очистка `trigger_spam`, `spam_tracker`, `spam_warned`, `ignored_until`.

## RP-система

Ответ на сообщение текстом из `rp-phrases.json`:
- `{mention1}` → автор сообщения
- `{mention2}` → цель (на чьё сообщение отвечают)

## Логирование

Настройка в `source/logging_settings.py`:
- Цветной вывод в консоль (уровни: INFO=зелёный, WARNING=жёлтый, ERROR=красный, CRITICAL=красный+жёлтый фон)
- `StatusCodeHandler` для backend-логов — переопределяет уровень на ERROR при 5xx и WARNING при 4xx

## Типизация (mypy)

Проект аннотирован. Конфигурация в `pyproject.toml`: `strict=false`, `disallow_untyped_defs=true`, `ignore_missing_imports=true`.

## Тестирование

pytest настроен в `pyproject.toml` с `asyncio_mode = "auto"`.

## Контейнеризация и деплой

- `Dockerfile` — образ на основе python:3.11-slim
- `docker-compose.yml` — сервис с env vars и volume для данных
- `pibot.service` — systemd unit для управления через systemctl
- `setup.sh` + `launchbot.sh` — bare-metal деплой
