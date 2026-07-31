# Architecture

PiBot is an aiogram 3 Telegram bot with a service-layer design. The bot
handles moderation (filters, anti-flood, raid protection), roleplay/phrase
replies, AI chat completions and a set of admin commands.

## Module layout

```
source/
├── pibot.py            Bot container: wiring, middleware, lifecycle
├── command_router.py   Command name → handler dispatch + admin handlers
├── chat_service.py     Chat-level logic: filter, anti-flood, phrases, RP
├── ai_service.py       AI providers, prompt building, history, tokens
├── user_service.py     Global bans, username map, ranks
├── persistence.py      Persistence ABC + SQLite implementation
├── anti_raid.py        Raid protection (slow mode, auto-kick new joins)
├── telemetry.py        Async telemetry store with rotation
├── filtering.py        Text filter compilation and matching
├── greeter.py          /start command
├── logging_settings.py Console/file logging setup (JSON file logs)
├── constants.py        Shared constants and tuning knobs
└── utils.py            Rate limiter, permissions, mention helpers
```

## Data flow

1. A `Message` arrives → `PiBotMiddleware.__call__` builds per-message
   context:
   - loads `ChatData` for the chat (cached),
   - tracks the message id, records the known chat and username,
   - builds `bot_data` (bans, known chats, username map, LLM provider),
   - applies filter and anti-flood checks (admins bypass).
2. `PiBot.handle_message`:
   - `_pre_check` rejects old messages and banned users,
   - `CommandRouter.handle_command` handles `пибот ...` commands,
   - otherwise chat_service handles RP commands, trigger phrases,
   - finally AI service handles mentions/private queries and, with low
     probability, a random reply to a recent group message.

## Persistence

`SQLitePersistence` keeps an in-memory working set (bans, known chats,
username map, per-chat `ChatData`) and persists it in batches.

- Writes are buffered in `_pending_*` dicts and flushed in a single
  transaction by `flush()` (commit first, clear pending only on success).
- `ChatData` holds persistent fields (`ranks`, `ignored_until`, `config`)
  loaded in one `json_group_array` query plus transient in-memory state
  (`message_ids`, spam trackers).
- A background periodic flush task (`start_periodic_flush`) and an hourly
  `_expire_old_rows` cleanup prune expired rows, invalidate affected caches
  and VACUUM on a separate connection.
- DB operations are retried (`DB_RETRY_MAX_ATTEMPTS`) on lock/busy errors.
- Schema uses foreign keys; child tables reference `known_chats`, which is
  backfilled on startup.

## AI service

- Providers (Groq, OpenRouter) are wrapped in `AIBackend` with retry and
  429 handling.
- Private-chat history is persisted in the `chat_history` table and cached
  in a TTL `HistoryStore`; group history is in-memory only.
- Token counting is cached with `functools.lru_cache`; context is trimmed
  to `HISTORY_TOKEN_BUDGET` before each request.

## Observability

- Logs go to stdout (colored console formatter) and a rotating-behavior
  file handler producing JSON lines (`JsonFormatter`).
- `Telemetry` writes AI request metrics asynchronously (`aiofiles`) and
  rotates the file past `TELEMETRY_MAX_BYTES`.

## Security

- `git clone` URLs are validated (`_validate_git_url`) against an allowlist
  of schemes; `; | & -- \n \r` and option-style arguments are rejected.
- Repo size (100 MB) and archive size (40 MB) limits abort the clone flow.
- Numeric command parameters (nuke count, mute duration, rank) are bounded.
- A pid lock (`fcntl`) prevents duplicate bot instances.
