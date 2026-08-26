import logging
import os

import anthropic

import config

logger = logging.getLogger(__name__)

_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)

if not os.path.exists(config.SYSTEM_PROMPT_PATH):
    raise RuntimeError(
        f"{config.SYSTEM_PROMPT_PATH} belum ada. Copy dari "
        f"{config.SYSTEM_PROMPT_PATH}.example dulu (deploy/setup.sh dan deploy/update.sh "
        "sebenarnya ngelakuin ini otomatis), atau atur lewat config_app.py."
    )

with open(config.SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
    _SYSTEM_PROMPT = f.read()


async def polish_signal(raw_text: str) -> str | None:
    """Kirim pesan mentah ke Claude untuk dirapikan.

    Return None kalau LLM menandai pesan sebagai bukan sinyal (SKIP).
    """
    response = await _client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": raw_text}],
    )
    text_blocks = [block.text for block in response.content if block.type == "text"]
    result = "".join(text_blocks).strip()

    if result.upper() == "SKIP":
        return None
    return result
