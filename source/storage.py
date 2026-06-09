import asyncio
import json
import logging
from pathlib import Path

from telegram import ChatPermissions

logger = logging.getLogger(__name__)

BASE = Path(__file__).parent.parent
TOKEN_PATH = BASE / "env" / "telegram-token"
PHRASES_PATH = BASE / "bot-data" / "phrases.json"
BOTINFO_PATH = BASE / "bot-data" / "botinfo.md"
CHANGELOG_PATH = BASE / "bot-data" / "changelog.md"
COMMANDLIST_PATH = BASE / "info" / "command-list.md"
RP_COMMANDS_PATH = BASE / "bot-data" / "rp-phrases.json"
BANNED_USERS_PATH = BASE / "bot-data" / "banned-users.json"
DEV_IDS_PATH = BASE / "env" / "dev-ids.json"
GROQ_KEY_PATH = BASE / "env" / "groq-key"
PERSONALITY_PATH = BASE / "bot-data" / "personality.md"

MAX_TRACKED_MESSAGES = 1000
DELETE_BATCH_SIZE = 100
MAX_MESSAGE_AGE = 120
TRIGGER_SPAM_WINDOW = 60
TRIGGER_SPAM_LIMIT = 5
TRIGGER_SPAM_MUTE = 120
ANTISPAM_WINDOW = 1.0
ANTISPAM_LIMIT = 5
ANTISPAM_MUTE_DURATION = 60
LLM_HISTORY_LIMIT = 20
LLM_GLOBAL_LIMIT = 30
LLM_GLOBAL_PERIOD = 60.0

CHANCE_TRIGGER = "пибот инфа"
WELCOME_MESSAGE = "Я вернулась"

RANK_OWNER = 1
RANK_ADMIN_PLUS = 2
RANK_ADMIN = 3
RANK_MEMBER = 4

RANK_NAMES = {
    RANK_ADMIN_PLUS: "Admin+",
    RANK_ADMIN: "Admin",
    RANK_MEMBER: "Member",
}

_phrases: dict[str, str] = {}
_rp_commands: dict[str, str] = {}
_banned_users: set[int] = set()
_dev_ids: set[int] = set()

_cached_botinfo: str = ""
_cached_changelog: str = ""
_cached_commandlist: str = ""

ban_lock = asyncio.Lock()


def load_phrases() -> dict[str, str]:
    if PHRASES_PATH.exists():
        with open(PHRASES_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_text_file(path: Path) -> str:
    if path.exists():
        return path.read_text().strip()
    return ""


def load_rp_commands() -> dict[str, str]:
    if RP_COMMANDS_PATH.exists():
        with open(RP_COMMANDS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_banned_users() -> set[int]:
    if BANNED_USERS_PATH.exists():
        with open(BANNED_USERS_PATH) as f:
            return set(json.load(f))
    return set()


def save_banned_users(ids: set[int]) -> None:
    with open(BANNED_USERS_PATH, "w") as f:
        json.dump(sorted(ids), f)


def load_dev_ids() -> set[int]:
    if DEV_IDS_PATH.exists():
        with open(DEV_IDS_PATH) as f:
            return set(json.load(f))
    return set()


def _cache_text_files() -> None:
    global _cached_botinfo, _cached_changelog, _cached_commandlist
    _cached_botinfo = load_text_file(BOTINFO_PATH)
    _cached_changelog = load_text_file(CHANGELOG_PATH)
    _cached_commandlist = load_text_file(COMMANDLIST_PATH)


def load_data() -> None:
    global _phrases, _rp_commands, _banned_users, _dev_ids
    _phrases = load_phrases()
    _rp_commands = load_rp_commands()
    _banned_users = load_banned_users()
    _dev_ids = load_dev_ids()
    _cache_text_files()


NO_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_other_messages=False,
    can_send_polls=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=False,
    can_pin_messages=False,
)

ALL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_other_messages=True,
    can_send_polls=True,
    can_add_web_page_previews=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
)
