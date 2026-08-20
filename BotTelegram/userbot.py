import asyncio
import io
import logging

from telegram import Bot
from telegram.error import BadRequest
from telegram.request import HTTPXRequest
from telethon import TelegramClient, events

import config
from llm import polish_signal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("signal-forwarder")

client = TelegramClient(config.SESSION_PATH, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
bot = Bot(
    token=config.TARGET_BOT_TOKEN,
    request=HTTPXRequest(connect_timeout=20, read_timeout=60, write_timeout=60, pool_timeout=20),
)

# Mapping ID pesan sumber -> ID pesan yang di-post bot di channel tujuan.
# Dipakai supaya reply di grup sumber bisa ikut jadi reply di channel tujuan.
# Catatan: cuma tersimpan di memory, jadi reset kalau proses ini di-restart.
sent_message_map: dict[int, int] = {}


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

    llm_input = raw_text
    target_reply_id = None
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        reply_text = (reply_msg.raw_text or "").strip() if reply_msg else ""
        if reply_text:
            llm_input = (
                f"[PESAN ASLI YANG DI-REPLY]\n{reply_text}\n\n"
                f"[PESAN UPDATE/BALASAN]\n{raw_text}"
            )
        if reply_msg:
            target_reply_id = sent_message_map.get(reply_msg.id)

    logger.info("Pesan baru diterima, memproses ke LLM...")

    try:
        polished = await polish_signal(llm_input)
    except Exception:
        logger.exception("Gagal memproses pesan lewat LLM, pesan dibatalkan.")
        return

    if polished is None:
        logger.info("LLM menandai pesan sebagai bukan sinyal, di-skip.")
        return

    photo_bytes = await event.download_media(file=bytes) if event.photo else None

    async def _send(reply_id):
        if photo_bytes is not None:
            return await bot.send_photo(
                chat_id=config.TARGET_CHAT,
                photo=io.BytesIO(photo_bytes),
                caption=polished,
                reply_to_message_id=reply_id,
            )
        return await bot.send_message(
            chat_id=config.TARGET_CHAT,
            text=polished,
            reply_to_message_id=reply_id,
        )

    try:
        try:
            sent = await _send(target_reply_id)
        except BadRequest:
            if target_reply_id is None:
                raise
            logger.warning("Pesan yang mau di-reply sudah tidak ada, kirim ulang tanpa reply.")
            sent = await _send(None)
        sent_message_map[event.id] = sent.message_id
        logger.info("Sinyal berhasil diteruskan ke channel tujuan.")
    except Exception:
        logger.exception("Gagal mengirim pesan ke channel tujuan.")


async def main():
    await client.start()
    logger.info("Userbot aktif, listening pesan baru dari %s ...", config.SOURCE_CHAT)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
