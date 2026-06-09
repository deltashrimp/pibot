import logging
import os

from openai import AsyncOpenAI

from storage import GROQ_KEY_PATH, PERSONALITY_PATH

logger = logging.getLogger(__name__)

llm_client: AsyncOpenAI | None = None
personality_prompt: str = ""


def init_clients() -> None:
    global llm_client
    llm_client = None
    try:
        groq_key = os.getenv("GROQ_API_KEY") or GROQ_KEY_PATH.read_text().strip()
        if groq_key and groq_key != "YOUR-GROQ-API-KEY-HERE":
            llm_client = AsyncOpenAI(
                api_key=groq_key, base_url="https://api.groq.com/openai/v1"
            )
    except Exception as e:
        logger.error("[Init] Groq client error: %s", e)


async def ask_llm(history: list[dict]) -> str:
    if not personality_prompt or llm_client is None:
        return ""
    try:
        response = await llm_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": personality_prompt}] + history,
            max_tokens=300,
            temperature=0.9,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        code = (
            getattr(e, "status_code", None)
            or getattr(e, "code", None)
            or getattr(e, "status", None)
        )
        logger.error("[LLM error] %s: %s", type(e).__name__, e)
        if code:
            return f"__API_ERR:{code}"
        return "__API_ERR"
