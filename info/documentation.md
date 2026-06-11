# Архитектура и функциональность Pibot

## Структура проекта

```
pibot/
├── bot-data/                   # Данные, загружаемые при старте
│   ├── phrases.json            # Триггер-фразы и ответы
│   ├── public-phrases.json     # Шаблон phrases.json для setup.sh
│   ├── rp-phrases.json         # RP-команды (ответом на сообщение)
│   ├── banned-users.json       # ID заблокированных пользователей
│   ├── personality.md          # Системный промпт для LLM
│   ├── personality-public.md   # Публичная версия промпта
│   ├── botinfo.md              # Вывод по "пибот био"
│   ├── changelog.md            # Вывод по "пибот обновы"
│   └── public-botinfo.md       # Шаблон botinfo.md для setup.sh
├── env/                        # Неотслеживаемые конфиги (gitignored)
│   └── dev-ids.json            # ID разработчиков (int[])
├── info/                       # Документация и справка
│   ├── command-list.md         # Список команд (вывод по "пибот команды")
│   ├── documentation.md        # Эта документация
│   ├── documentation.json      # Документация для LLM
│   ├── full-changelog.md       # Полный список изменений
│   └── future-features.md      # Планируемые функции
├── important/                  # Внутренние утилиты (gitignored)
│   ├── logging_settings.py     # Логирование: цветной вывод, цензура токена, файл, JSONFormatter
│   ├── code-review*.md         # Результаты ревью кода
│   └── logs/                   # Файлы логов
├── source/
│   ├── pibot.py                # Основной код бота (класс PiBot, ~1156 строк)
│   └── persistence.py          # SQLite persistence (BasePersistence, 172 строки)
├── .env                        # Переменные окружения (gitignored, создаётся setup.sh)
├── .env.example                # Шаблон переменных окружения
├── Dockerfile                  # Контейнеризация
├── docker-compose.yml          # Docker Compose
├── pibot.service               # systemd unit
├── pyproject.toml              # Конфигурация проекта и mypy
├── setup.sh                    # Скрипт развёртывания
├── launchbot.sh                # Скрипт запуска
├── requirements.txt            # Зависимости (точные версии)
├── README.md                   # Главный README
├── README-EN.md                # README на английском
└── README-RU.md                # README на русском
```

## Персистентность

Данные бота хранятся в SQLite (`source/bot_data.db`). Реализация — собственный класс `SQLitePersistence` в `source/persistence.py`, наследующий `BasePersistence` из python-telegram-bot.

Данные чатов (`chat_data`), данные бота (`bot_data`), данные пользователей (`user_data`) сериализуются через JSON с поддержкой `set` и `deque` (через `SetEncoder` + `_object_hook`).

## Класс PiBot

Весь функционал инкапсулирован в класс `PiBot` (`source/pibot.py:259`). При инициализации:

1. Загружает `phrases.json`, `rp-phrases.json`, `dev-ids.json` в память
2. Инициализирует LLM-клиента (Groq) если есть ключ и personality.md
3. Регистрирует команды через `_register_commands()`
4. Создаёт `Application` с SQLitePersistence и двумя MessageHandler (group -1 для трекинга, group 0 для обработки)

## Обработка сообщений (`PiBot.handle_message`)

Сообщение проходит через цепочку обработчиков. Каждый этап может вернуть управление:

1. **Pre-check** (`_pre_check()`) — блокировка забаненных, фильтр возраста сообщения ( >120 сек)
2. **Команды** (`_handle_command()`) — `пибот <команда>`, проверка ранга или dev-статуса
3. **Игнор** (`is_user_ignored()`) — проверка таймаута триггер-спама
4. **RP-команды** (`_handle_rp()`) — ответ на сообщение, совпадение с RP-фразой
5. **Триггер-фразы** (`_handle_phrase()`) — точное совпадение в `phrases.json`
6. **Chance trigger** (`_handle_chance()`) — `пибот инфа` → случайное число 0-100
7. **AI-ответ** (`_handle_llm()`) — @упоминание бота → Groq LLama 3.3 70B

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

Проверка ранга: `user_rank <= command.value` даёт доступ.
Назначить можно только ранг ниже своего (выше числом).
Нельзя назначить ранг 4 Telegram-админу. Нельзя назначить ранг 2 или 3 не-админу.

### Команды и их значения

| Команда | Value | Мин. ранг | Описание |
|---------|-------|-----------|----------|
| `кинь [в гулаг] @user` | 1 | Owner | Telegram-бан + запись в bot_data |
| `выкинь @user` | 1 | Owner | Алиас для кинь |
| `верни @user` | 1 | Owner | Telegram-анбан + удаление из bot_data |
| `ранг n для @user` | 1 | Owner | Изменить ранг (n = 2, 3, 4) |
| `сотри n` | 2 | Admin+ | Удалить n последних сообщений |
| `кикни @user` | 2 | Admin+ | Кик пользователя |
| `мут @user [n]` | 3 | Admin | Мут на n минут (минимум 0.5) |
| `размут @user` | 3 | Admin | Размут (без проверки иммунитета) |
| `био` | 4 | Member | Информация о боте |
| `обновы` | 4 | Member | Список изменений |
| `команды` | 4 | Member | Список команд |
| `ранги` | 4 | Member | Пользователи с особыми рангами |
| `заблокируй <id>` | dev_only | DEV_ID | Блокировка в bot_data (без Telegram) |

### DEV_ID команды

Команда `заблокируй` доступна только пользователям из `env/dev-ids.json`.
Ранговая проверка для неё не применяется.

### Hard block (`self.banned_users`)

Хранится в памяти (`self.banned_users`, set[int]), инициализируется из `bot_data['banned_users']` в `post_init`, синхронизируется при изменениях:
- `кинь [в гулаг]` — добавляет ID + Telegram-ban с revoke_messages=True
- `верни` — удаляет ID + Telegram-unban с only_if_banned=True
- `заблокируй` — добавляет ID без Telegram-бана (dev_only)

## AI-функции

- Groq (Llama 3.3 70B Versatile) через OpenAI-совместимый SDK
- Ключ API читается из переменной окружения `GROQ_KEY` (из `.env` файла)
- Системный промпт из `bot-data/personality.md`
- При @упоминании бота или в личных сообщениях — контекст в LLM
- Rate limit: 3 вызова в минуту на чат
- Timeout: 15 секунд
- Retry: до 3 попыток:
  - `RateLimitError` (429) — экспоненциальная задержка (2^attempt секунд)
  - `APITimeoutError` / `APIConnectionError` — 1 секунда, на 3-й попытке возвращает "AI временно недоступен"
  - 5xx ошибки — экспоненциальная задержка (2^attempt секунд)
  - Остальные ошибки — немедленный возврат `__API_ERR:[code]`

## Конфигурация при старте

Перед запуском `main()` вызывает `load_dotenv()`, затем `load_config()`:
- Читает `TELEGRAM_TOKEN` и `GROQ_KEY` из переменных окружения
- Проверяет, что токен и ключ не пусты и не равны placeholder-значениям
- При ошибке выводит понятное сообщение и завершает работу

## Антиспам

### RateLimiter (глобальный)
Sliding window: 5 вызовов/сек. При превышении сообщение дропается.

### Age filter
Сообщения старше 120 секунд игнорируются. Команды с "пибот" не блокируются.

### Trigger phrase spam filter
>5 одинаковых триггер-фраз за 60 секунд: ответ "Ой всё", игнор на 2 минуты.

### Telegram mute
При спаме (>5 сообщений/сек, не триггер-фразы и не RP):
1. 6-9 сообщений: предупреждение "пожалуйста, не флуди" (один раз через spam_warned)
2. >9 сообщений: `restrictChatMember` на 60 секунд
3. Админы, Admin+ и Owner не подвержены спам-фильтру

### Трекинг сообщений
- `message_ids` хранится в `deque` (не список), защищён `asyncio.Lock` на чат
- `self.msg_locks` и `self.llm_rate_limiters` очищаются раз в час от мёртвых чатов через `_cleanup_caches()`

## RP-система

Ответ на сообщение текстом из `rp-phrases.json`:
- `{mention1}` → автор сообщения
- `{mention2}` → цель (на чьё сообщение отвечают)

## Логирование

Настройка в `important/logging_settings.py`:
- Цветной вывод в консоль (INFO=зелёный, WARNING=жёлтый, ERROR=красный, CRITICAL=красный+жёлтый фон)
- Цензура токена бота в логах (замена на `-BOT-TOKEN-HERE-`)
- Файловый лог `info/logs.log` с почасовой ротацией, записываются все уровни кроме INFO
- `StatusCodeHandler` для uvicorn — переопределяет уровень на ERROR при 5xx и WARNING при 4xx
- `JSONFormatter` — форматирование логов в JSON для интеграции с системами сбора логов

## Типизация (mypy)

Проект аннотирован. Конфигурация в `pyproject.toml`: strict=false, disallow_untyped_defs=true.

## Тестирование

pytest настроен в `pyproject.toml` с `asyncio_mode = "auto"`.

## Контейнеризация

- `Dockerfile` — образ на основе python:3.11-slim
- `docker-compose.yml` — сервис с переменными окружения и volume для данных
- `pibot.service` — systemd unit для управления через systemctl
