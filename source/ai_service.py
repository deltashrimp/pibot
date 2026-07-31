"""AI provider handling, prompt building and conversation history."""

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Optional

from aiogram.enums import MessageEntityType
from aiogram.types import Message
from groq import RateLimitError as GroqRateLimitError
from openai import RateLimitError as OpenAIRateLimitError

from constants import (
    AI_MAX_HISTORY,
    AI_PROVIDER_OFF,
    AI_RETRY_BASE_DELAY,
    AI_RETRY_MAX_ATTEMPTS,
    GROUP_HISTORY_MAX,
    GROQ_MODEL,
    HISTORY_MAX_CHATS,
    HISTORY_TOKEN_BUDGET,
    HISTORY_TTL,
    MIN_RANDOM_MSG_LENGTH,
    OPENROUTER_MODEL,
    PRIVATE_HISTORY_MAX,
    RANDOM_REPLY_CHANCE,
)
from persistence import ChatData
from utils import RateLimiter

if TYPE_CHECKING:
    from pibot import PiBot

logger = logging.getLogger(__name__)

_TOKEN_ENCODING: Any | None = None
try:
    import tiktoken as _tiktoken

    _TOKEN_ENCODING = _tiktoken.get_encoding("cl100k_base")
except Exception:
    pass


@lru_cache(maxsize=10000)
def count_tokens(text: str) -> int:
    """Return the token count for ``text`` (cached)."""
    if _TOKEN_ENCODING is not None:
        try:
            n = len(_TOKEN_ENCODING.encode(text))
            if n:
                return n
        except Exception:
            pass
    return max(1, len(text) // 4)


class HistoryStore:
    """Bounded, TTL-based in-memory history store for chats."""

    def __init__(
        self, maxsize: int = HISTORY_MAX_CHATS, ttl: float = HISTORY_TTL
    ) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._items: dict[int, tuple[float, list[Any]]] = {}

    def _expire(self, now: float) -> None:
        for chat_id in [
            cid
            for cid, (ts, _) in self._items.items()
            if now - ts > self.ttl
        ]:
            del self._items[chat_id]

    def _touch(self, chat_id: int, now: float) -> None:
        item = self._items[chat_id]
        if item[0] != now:
            self._items[chat_id] = (now, item[1])

    def _make_room(self, now: float) -> None:
        while len(self._items) >= self.maxsize:
            self._items.pop(next(iter(self._items)))

    def get(self, chat_id: int) -> list[Any] | None:
        now = time.monotonic()
        self._expire(now)
        if chat_id not in self._items:
            return None
        self._touch(chat_id, now)
        return self._items[chat_id][1]

    def get_or_create(self, chat_id: int) -> list[Any]:
        now = time.monotonic()
        self._expire(now)
        if chat_id not in self._items:
            self._make_room(now)
            self._items[chat_id] = (now, [])
        else:
            self._touch(chat_id, now)
        return self._items[chat_id][1]

    def put(self, chat_id: int, value: list[Any]) -> None:
        now = time.monotonic()
        self._expire(now)
        self._make_room(now)
        self._items[chat_id] = (now, value)

    def prune(self) -> None:
        self._expire(time.monotonic())


@dataclass
class AIResponse:
    """Result of a single AI generation request."""

    content: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class AIBackend:
    """Wraps an LLM provider client with retry logic."""

    def __init__(
        self,
        name: str,
        display_name: str,
        client: Any | None,
        model: str,
        rate_limit_error: type[Exception] | None = None,
    ) -> None:
        self.name = name
        self.display_name = display_name
        self.client = client
        self.model = model
        self._rate_limit_error = rate_limit_error

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def generate(self, messages: list[dict]) -> AIResponse:
        """Call the provider, retrying transient errors with backoff."""
        client = self.client
        if client is None:
            logger.error("[%s] Client is not configured", self.name)
            return AIResponse(
                content="⚠️ ИИ не настроен", model=self.model
            )
        last_error: Exception | None = None
        for attempt in range(AI_RETRY_MAX_ATTEMPTS):
            try:
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=messages,  # type: ignore[arg-type]
                    temperature=0.7,
                    max_tokens=512,
                )
                if not response or not response.choices:
                    logger.error("[%s] Invalid API response: %s", self.name, response)
                    return AIResponse(
                        content="⚠️ Ошибка при получении ответа от ИИ",
                        model=self.model,
                    )
                content = (
                    response.choices[0].message.content or "⚠️ ИИ ничего не ответил"
                )
                usage = response.usage
                return AIResponse(
                    content=content,
                    model=response.model,
                    prompt_tokens=usage.prompt_tokens if usage else None,
                    completion_tokens=usage.completion_tokens if usage else None,
                    total_tokens=usage.total_tokens if usage else None,
                )
            except Exception as e:
                if self._rate_limit_error and isinstance(e, self._rate_limit_error):
                    raise
                last_error = e
                if attempt < AI_RETRY_MAX_ATTEMPTS - 1:
                    delay = AI_RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        "[%s] API error (attempt %d/%d): %s. Retrying in %.1fs...",
                        self.name,
                        attempt + 1,
                        AI_RETRY_MAX_ATTEMPTS,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
        logger.error(
            "[%s] API error after %d attempts: %s",
            self.name,
            AI_RETRY_MAX_ATTEMPTS,
            last_error,
        )
        raise last_error  # type: ignore[misc]


class AIService:
    """Encapsulates AI provider selection, prompts and history."""

    def __init__(self, pibot: "PiBot", groq_key: str = "", openrouter_key: str = "") -> None:
        self.pibot = pibot
        self.providers: dict[str, AIBackend] = {}
        self.conversation_history = HistoryStore()
        self.chat_history = HistoryStore()
        self.ai_limiter = RateLimiter(max_calls=2, period=60.0)
        self._init_providers(groq_key, openrouter_key)

    def _init_providers(self, groq_key: str, openrouter_key: str) -> None:
        """Set up the configured AI providers (Groq, OpenRouter)."""
        from groq import AsyncGroq
        from openai import AsyncOpenAI

        groq_client = AsyncGroq(api_key=groq_key) if groq_key else None
        self.providers["groq"] = AIBackend(
            name="groq",
            display_name="Groq",
            client=groq_client,
            model=GROQ_MODEL,
            rate_limit_error=GroqRateLimitError,
        )

        or_client = (
            AsyncOpenAI(
                api_key=openrouter_key,
                base_url="https://openrouter.ai/api/v1",
            )
            if openrouter_key
            else None
        )
        self.providers["openrouter"] = AIBackend(
            name="openrouter",
            display_name="OpenRouter",
            client=or_client,
            model=OPENROUTER_MODEL,
            rate_limit_error=OpenAIRateLimitError,
        )

        enabled = [p for p in self.providers.values() if p.enabled]
        logger.info(
            "AI: инициализировано %d провайдеров"
            if enabled
            else "AI: ни один провайдер не настроен",
            len(enabled),
        )

    async def ensure_provider(self) -> None:
        """Persist the first enabled provider as the current one, if unset."""
        name = await self.pibot.persistence.get_bot_config("llm_provider", "")
        if name == AI_PROVIDER_OFF:
            return
        if name in self.providers and self.providers[name].enabled:
            return
        for p in self.providers.values():
            if p.enabled:
                await self.pibot.persistence.set_bot_config("llm_provider", p.name)
                break
        else:
            logger.warning("No enabled AI provider available")

    def get_current_provider(self, bot_data: dict) -> AIBackend | None:
        """Return the configured/available provider, or None."""
        name = bot_data.get("llm_provider", "")
        if name == AI_PROVIDER_OFF:
            return None
        provider = self.providers.get(name)
        if provider and provider.enabled:
            return provider
        for p in self.providers.values():
            if p.enabled:
                return p
        logger.warning("No enabled AI provider found")
        return None

    def add_group_message(self, chat_id: int, message: Message) -> None:
        """Record a group message into the in-memory chat history."""
        user = message.from_user
        if not user or not message.text:
            return
        mention = (
            user.username
            or user.first_name
            or str(user.id)
        )
        history = self.chat_history.get_or_create(chat_id)
        history.append({
            "username": mention,
            "text": message.text,
            "message_id": message.message_id,
        })
        if len(history) > GROUP_HISTORY_MAX:
            history[:] = history[-GROUP_HISTORY_MAX:]

    async def is_bot_mentioned(self, message: Message) -> bool:
        if not message.entities:
            return False
        text = message.text
        if text is None:
            return False
        for entity in message.entities:
            if entity.type == MessageEntityType.MENTION:
                mention = text[entity.offset : entity.offset + entity.length]
                if mention[1:].lower() == self.pibot.bot_username:
                    return True
            elif entity.type == MessageEntityType.TEXT_MENTION:
                if entity.user and entity.user.id == self.pibot.bot_id:
                    return True
        return False

    def _strip_mention(self, message: Message) -> str:
        text = message.text
        if text is None:
            return ""
        if not message.entities:
            return text.strip()
        for entity in sorted(message.entities, key=lambda e: e.offset, reverse=True):
            if entity.type == MessageEntityType.MENTION:
                mention = text[entity.offset : entity.offset + entity.length]
                if mention[1:].lower() == self.pibot.bot_username:
                    text = text[: entity.offset] + text[entity.offset + entity.length :]
            elif entity.type == MessageEntityType.TEXT_MENTION:
                if entity.user and entity.user.id == self.pibot.bot_id:
                    text = text[: entity.offset] + text[entity.offset + entity.length :]
        return text.strip()

    async def _get_private_history(self, chat_id: int) -> list[dict]:
        history = self.conversation_history.get(chat_id)
        if history is None:
            history = await self.pibot.persistence.load_chat_history(chat_id)
            self.conversation_history.put(chat_id, history)
        return history

    def _trim_context_lines(self, lines: list[str]) -> list[str]:
        result = list(lines)
        while result:
            if sum(count_tokens(line) for line in result) <= HISTORY_TOKEN_BUDGET:
                break
            result.pop(0)
        return result

    def _trim_to_token_budget(self, messages: list[dict]) -> list[dict]:
        total = sum(count_tokens(str(m.get("content", ""))) for m in messages)
        if total <= HISTORY_TOKEN_BUDGET:
            return messages
        system = messages[0]
        query = messages[-1]
        history = messages[1:-1]
        while history and total > HISTORY_TOKEN_BUDGET:
            dropped = history.pop(0)
            total -= count_tokens(str(dropped.get("content", "")))
        return [system] + history + [query]

    async def _build_ai_messages(self, message: Message, query: str) -> list[dict]:
        messages: list[dict] = [
            {"role": "system", "content": self.pibot.personality}
        ]

        if message.chat.type == "private":
            history = await self._get_private_history(message.chat.id)
            messages.extend(history)
        else:
            history = self.chat_history.get(message.chat.id) or []
            context_lines = [
                f"@{entry['username']} said: {entry['text']}"
                for entry in history[-AI_MAX_HISTORY:]
            ]
            context_lines = self._trim_context_lines(context_lines)
            if context_lines:
                messages.append(
                    {
                        "role": "system",
                        "content": "Recent chat history:\n" + "\n".join(context_lines),
                    }
                )

        messages.append({"role": "user", "content": query})
        return self._trim_to_token_budget(messages)

    async def handle_ai(
        self, message: Message, chat_data: ChatData, bot_data: dict
    ) -> bool:
        """Handle an AI request; returns True if the message was handled."""
        provider = self.get_current_provider(bot_data)
        if not provider or not provider.enabled:
            logger.warning("No available AI provider for message handling")
            return False

        user = message.from_user
        assert user is not None

        if message.chat.type in ("group", "supergroup"):
            if not await self.is_bot_mentioned(message):
                reply = message.reply_to_message
                if not (
                    reply
                    and reply.from_user
                    and reply.from_user.id == self.pibot.bot_id
                ):
                    return False

        if not await self.ai_limiter.acquire():
            await self.pibot.safe_reply(
                message,
                "⚠️ Лимит апиииииии. Жди минуту ",
            )
            return True

        query = (
            self._strip_mention(message)
            if message.chat.type in ("group", "supergroup")
            else (message.text or "").strip()
        )
        if not query:
            return False

        ai_messages = await self._build_ai_messages(message, query)

        start = time.monotonic()
        for attempt in range(AI_RETRY_MAX_ATTEMPTS):
            try:
                ai_response = await provider.generate(ai_messages)
                break
            except (GroqRateLimitError, OpenAIRateLimitError):
                if attempt < AI_RETRY_MAX_ATTEMPTS - 1:
                    delay = AI_RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        "[%s] 429 Rate Limit (attempt %d/%d). Retrying in %.1fs...",
                        provider.name,
                        attempt + 1,
                        AI_RETRY_MAX_ATTEMPTS,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                duration = round((time.monotonic() - start) * 1000)
                logger.warning(
                    "[%s] 429 Rate Limit after %d attempts",
                    provider.name,
                    AI_RETRY_MAX_ATTEMPTS,
                )
                await self.pibot.telemetry.record(
                    {
                        "user_id": user.id,
                        "username": user.username,
                        "chat_id": message.chat.id,
                        "chat_type": message.chat.type,
                        "provider": provider.name,
                        "model": provider.model,
                        "success": False,
                        "duration_ms": duration,
                        "error": "RateLimitError",
                    }
                )
                await self.pibot.safe_reply(
                    message,
                    "⚠️ Ты достиг лимита апи, задрот",
                )
                return True
            except Exception as e:
                duration = round((time.monotonic() - start) * 1000)
                logger.error(
                    "[%s] API error after %d attempts: %s\n"
                    "Context: %s",
                    provider.name,
                    AI_RETRY_MAX_ATTEMPTS,
                    e,
                    json.dumps(ai_messages, ensure_ascii=False)[:500],
                    exc_info=True,
                )
                await self.pibot.telemetry.record(
                    {
                        "user_id": user.id,
                        "username": user.username,
                        "chat_id": message.chat.id,
                        "chat_type": message.chat.type,
                        "provider": provider.name,
                        "model": provider.model,
                        "success": False,
                        "duration_ms": duration,
                        "error": str(e),
                    }
                )
                await self.pibot.safe_reply(message, "⚠️ Ошибка при обращении к ИИ")
                return True
        else:
            return True

        duration = round((time.monotonic() - start) * 1000)
        answer = ai_response.content

        await self.pibot.telemetry.record(
            {
                "user_id": user.id,
                "username": user.username,
                "chat_id": message.chat.id,
                "chat_type": message.chat.type,
                "provider": provider.name,
                "model": ai_response.model,
                "success": True,
                "duration_ms": duration,
                "prompt_tokens": ai_response.prompt_tokens,
                "completion_tokens": ai_response.completion_tokens,
                "total_tokens": ai_response.total_tokens,
            }
        )

        if message.chat.type == "private":
            history = self.conversation_history.get_or_create(message.chat.id)
            history.append({"role": "user", "content": query})
            history.append({"role": "assistant", "content": answer})
            if len(history) > PRIVATE_HISTORY_MAX:
                history[:] = history[-PRIVATE_HISTORY_MAX:]
            user_id = user.id
            await self.pibot.persistence.buffer_chat_history(
                message.chat.id, user_id, "user", query
            )
            await self.pibot.persistence.buffer_chat_history(
                message.chat.id, user_id, "assistant", answer
            )

        await self.pibot.safe_reply(message, answer, disable_notification=True)
        return True

    async def handle_random_reply(
        self, message: Message, chat_data: ChatData, bot_data: dict
    ) -> None:
        """Randomly reply to an older message in the group with a low chance."""
        if message.chat.type not in ("group", "supergroup"):
            return

        user = message.from_user
        assert user is not None

        if random.random() >= RANDOM_REPLY_CHANCE:
            return

        history = self.chat_history.get(message.chat.id) or []
        replied = self.pibot.replied_to_ids.setdefault(message.chat.id, set())
        candidates = [
            entry for entry in history[-AI_MAX_HISTORY:]
            if len(entry["text"]) > MIN_RANDOM_MSG_LENGTH
            and entry["message_id"] != message.message_id
            and entry["message_id"] not in replied
        ]
        if not candidates:
            return

        provider = self.get_current_provider(bot_data)
        if not provider or not provider.enabled:
            return

        if not await self.ai_limiter.acquire():
            return

        chosen = random.choice(candidates)

        context_lines = [
            f"@{entry['username']} said: {entry['text']}"
            for entry in history[-AI_MAX_HISTORY:]
        ]
        context_lines = self._trim_context_lines(context_lines)
        ai_messages: list[dict[str, str]] = [
            {"role": "system", "content": self.pibot.personality},
        ]
        if context_lines:
            ai_messages.append({
                "role": "system",
                "content": "Recent chat history:\n" + "\n".join(context_lines),
            })
        ai_messages.append({
            "role": "user",
            "content": (
                f"Someone wrote in chat:\n"
                f"@{chosen['username']}: {chosen['text']}\n\n"
                f"Write a short relevant reply to this message as if you're a "
                f"chat participant. Keep it concise and natural."
            ),
        })
        ai_messages = self._trim_to_token_budget(ai_messages)

        start = time.monotonic()
        for attempt in range(AI_RETRY_MAX_ATTEMPTS):
            try:
                ai_response = await provider.generate(ai_messages)
                break
            except (GroqRateLimitError, OpenAIRateLimitError):
                if attempt < AI_RETRY_MAX_ATTEMPTS - 1:
                    delay = AI_RETRY_BASE_DELAY * (2**attempt) + random.uniform(0, 1)
                    logger.warning(
                        "[%s] 429 Rate Limit random reply (attempt %d/%d). "
                        "Retrying in %.1fs...",
                        provider.name,
                        attempt + 1,
                        AI_RETRY_MAX_ATTEMPTS,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "[%s] 429 Rate Limit random reply after %d attempts",
                    provider.name,
                    AI_RETRY_MAX_ATTEMPTS,
                )
                return
            except Exception as e:
                logger.error(
                    "[%s] API error random reply: %s", provider.name, e, exc_info=True
                )
                return
        else:
            return

        duration = round((time.monotonic() - start) * 1000)
        answer = ai_response.content

        await self.pibot.telemetry.record(
            {
                "user_id": user.id,
                "username": user.username,
                "chat_id": message.chat.id,
                "chat_type": message.chat.type,
                "provider": provider.name,
                "model": ai_response.model,
                "success": True,
                "duration_ms": duration,
                "prompt_tokens": ai_response.prompt_tokens,
                "completion_tokens": ai_response.completion_tokens,
                "total_tokens": ai_response.total_tokens,
                "random_reply": True,
            }
        )

        try:
            await self.pibot.bot.send_message(
                chat_id=message.chat.id,
                text=answer,
                reply_to_message_id=chosen["message_id"],
                disable_notification=True,
            )
            replied.add(chosen["message_id"])
        except Exception as e:
            logger.warning("[random_reply] Failed to send message: %s", e)
