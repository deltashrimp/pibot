# Pibot — Telegram Chat Bot

Multifunctional Telegram bot with phrase responses, RP commands, AI integration (Groq Llama), and moderation tools.

## Features

- **Phrase responses** — automatically replies to trigger phrases from `phrases.json`
- **RP commands** — interactive roleplay (hug, kiss, etc.) via reply
- **AI integration** — responds with context when the bot is @mentioned (Groq Llama 3.3 70B via OpenAI SDK)
- **Admin commands** — `nuke`, `mute`, `unmute`
- **Superuser commands** — `kick`, `ban`, `block`
- **Anti-spam** — rate limiter, age filter, trigger phrase spam protection, Telegram mute
- **Rank system** — 4-level access (Owner, Admin+, Admin, Member)

## Structure

```
pibot/
├── bot-data/         # JSON/MD data files (phrases, rp-commands, personality)
├── env/              # Developer IDs (gitignored)
├── info/             # Documentation and help files
├── important/        # Logging setup and internal tooling (gitignored)
├── source/
│   ├── pibot.py      # Main bot class (~1150 lines)
│   └── persistence.py # SQLite persistence
├── .env.example      # Environment variable template
├── Dockerfile        # Containerization
├── docker-compose.yml # Docker Compose
├── pibot.service     # systemd unit
├── setup.sh          # Deployment script
└── launchbot.sh      # Launch script
```

## Setup

1. Clone the repository to your server
2. Run `bash setup.sh` — creates config files, venv, and installs dependencies
3. Fill in `.env` with your bot token and API keys (TELEGRAM_TOKEN, GROQ_KEY)
4. Optionally edit `bot-data/personality.md` and `bot-data/botinfo.md`
5. Run `./launchbot.sh`

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
# Logs: journalctl -u pibot -f
```

## Commands

Full list in `info/command-list.md` or type `пибот команды` in chat.

## Dependencies

- python-telegram-bot (v22.7, with job-queue)
- openai (v1.70.0, AsyncOpenAI for Groq API)
- python-dotenv (v1.0.1)
