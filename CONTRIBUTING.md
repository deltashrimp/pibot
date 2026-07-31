# Contributing

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio mypy   # dev/test dependencies
cp .env.example .env   # set TELEGRAM_TOKEN, GROQ_KEY, OPENROUTER_KEY
```

## Running

```bash
./launchbot.sh          # dev launch
```

Deploy: `docker-compose up -d` or the systemd unit `pibot.service`.

## Checks

Run before submitting changes:

```bash
.venv/bin/python -m mypy source/
.venv/bin/python -m py_compile source/*.py
.venv/bin/python -m pytest tests/
```

`pyproject.toml` pins the mypy and pytest configuration (asyncio mode
`auto`; all tests in `tests/` are async-friendly).

## Code style

- Type hints on all public functions and methods (mypy must pass clean).
- Service layer owns business logic; `PiBot` and middleware only wire and
  dispatch.
- Persistence mutations go through `Persistence`; no direct SQL outside
  `persistence.py`.
- Buffered writes must be flushed via `flush()`; prefer
  `persistence.schedule_task(...)` for fire-and-forget background work so
  shutdown can await it.

## Adding a command

1. Add a handler method on `CommandRouter` with the signature
   `(message, chat_data, bot_data, params)`.
2. Register it in `_register_commands` with a rank
   (`RANK_*` constants; `0` = dev only).
3. Add tests in `tests/`.

## Testing

- `tests/test_persistence.py` — DB behaviour on a temp SQLite file.
- `tests/test_ai_service.py` — history store, token trimming, backends.
- `tests/test_command_router.py` — git URL validation.
- `tests/test_command_handlers.py` — command routing/permissions with
  stub message objects.
- `tests/test_telemetry.py` — async writes and rotation.
