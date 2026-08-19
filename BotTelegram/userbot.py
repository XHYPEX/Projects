import asyncio
import io
import logging

from telegram import Bot
from telethon import TelegramClient, events

import config
from llm import polish_signal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("signal-forwarder")

client = TelegramClient(config.SESSION_PATH, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
bot = Bot(token=config.TARGET_BOT_TOKEN)


@client.on(events.NewMessage(chats=config.SOURCE_CHAT))
async def on_new_message(event):
    raw_text = (event.raw_text or "").strip()

    if not raw_text:
        if event.media:
            logger.info("Pesan media tanpa teks/caption di-skip (belum didukung).")
        return

    if len(raw_text) < config.MIN_MESSAGE_LENGTH:
        logger.info("Pesan terlalu pendek, di-skip: %r", raw_text)
        return

    logger.info("Pesan baru diterima, memproses ke LLM...")

    try:
        polished = await polish_signal(raw_text)
    except Exception:
        logger.exception("Gagal memproses pesan lewat LLM, pesan dibatalkan.")
        return

    if polished is None:
        logger.info("LLM menandai pesan sebagai bukan sinyal, di-skip.")
        return

    try:
        if event.photo:
            photo_bytes = await event.download_media(file=bytes)
            await bot.send_photo(
                chat_id=config.TARGET_CHAT,
                photo=io.BytesIO(photo_bytes),
                caption=polished,
            )
        else:
            await bot.send_message(chat_id=config.TARGET_CHAT, text=polished)
        logger.info("Sinyal berhasil diteruskan ke channel tujuan.")
    except Exception:
        logger.exception("Gagal mengirim pesan ke channel tujuan.")


async def main():
    await client.start()
    logger.info("Userbot aktif, listening pesan baru dari %s ...", config.SOURCE_CHAT)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
