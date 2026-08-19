import logging

import anthropic

import config

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

with open(config.SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
    _SYSTEM_PROMPT = f.read()


async def polish_signal(raw_text: str) -> str | None:
    """Kirim pesan mentah ke Claude untuk dirapikan.

    Return None kalau LLM menandai pesan sebagai bukan sinyal (SKIP).
    """
    response = await _client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        temperature=0,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": raw_text}],
    )
    result = response.content[0].text.strip()

    if result.upper() == "SKIP":
        return None
    return result
