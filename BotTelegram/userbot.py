import asyncio
import io
import logging

from telegram import Bot
from telegram.error import BadRequest, RetryAfter
from telegram.request import HTTPXRequest
from telethon import TelegramClient, events, utils

import config
import state
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

# Sampai 5 pasangan source -> target (lihat routes.json / routes.json.example).
ROUTES: list[config.Route] = config.load_routes()

# Diisi saat startup (main()) dengan resolved chat ID numerik -> Route, supaya
# handler bisa nentuin pasangan target/whitelist yang benar per pesan masuk.
routes_by_chat_id: dict[int, config.Route] = {}

# Mapping (source_chat_id, ID pesan sumber) -> ID pesan yang di-post bot di channel tujuan.
# Dipakai supaya reply di grup sumber bisa ikut jadi reply di channel tujuan.
# Di-load dari disk saat startup dan disimpan lagi tiap ada pesan baru terkirim,
# supaya reply tetap ke-link meskipun proses ini di-restart.
sent_message_map: dict[tuple[int, int], int] = state.load_message_map()

MAX_FLOOD_RETRIES = 3


async def alert_admin(text: str) -> None:
    if not config.ALERT_CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=config.ALERT_CHAT_ID, text=f"⚠️ [BotTelegram] {text}")
    except Exception:
        logger.exception("Gagal kirim alert ke ALERT_CHAT_ID.")


@client.on(events.NewMessage(chats=[route.source_chat for route in ROUTES]))
async def on_new_message(event):
    route = routes_by_chat_id.get(event.chat_id)
    if route is None:
        return

    if route.sender_whitelist and event.sender_id not in route.sender_whitelist:
        return

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
            target_reply_id = sent_message_map.get((event.chat_id, reply_msg.id))

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
                chat_id=route.target_chat,
                photo=io.BytesIO(photo_bytes),
                caption=polished,
                reply_to_message_id=reply_id,
            )
        return await bot.send_message(
            chat_id=route.target_chat,
            text=polished,
            reply_to_message_id=reply_id,
        )

    async def _send_with_flood_retry(reply_id):
        for attempt in range(1, MAX_FLOOD_RETRIES + 1):
            try:
                return await _send(reply_id)
            except RetryAfter as e:
                wait_s = e.retry_after + 1
                logger.warning(
                    "Kena flood control Telegram, retry dalam %ss (percobaan %d/%d)...",
                    wait_s,
                    attempt,
                    MAX_FLOOD_RETRIES,
                )
                await asyncio.sleep(wait_s)
        return await _send(reply_id)

    try:
        try:
            sent = await _send_with_flood_retry(target_reply_id)
        except BadRequest:
            if target_reply_id is None:
                raise
            logger.warning("Pesan yang mau di-reply sudah tidak ada, kirim ulang tanpa reply.")
            sent = await _send_with_flood_retry(None)
        sent_message_map[(event.chat_id, event.id)] = sent.message_id
        state.save_message_map(sent_message_map)
        logger.info("Sinyal dari %s berhasil diteruskan ke %s.", event.chat_id, route.target_chat)
    except Exception as e:
        logger.exception("Gagal mengirim pesan ke channel tujuan.")
        await alert_admin(
            f"Gagal kirim sinyal dari {event.chat_id} ke {route.target_chat}: {e}"
        )


async def main():
    await client.start()

    for route in ROUTES:
        entity = await client.get_entity(route.source_chat)
        chat_id = utils.get_peer_id(entity)
        if chat_id in routes_by_chat_id:
            raise RuntimeError(
                f"routes.json: dua pasangan pakai grup sumber yang sama ({route.source_chat}) "
                "-- tiap pasangan harus punya sumber unik."
            )
        routes_by_chat_id[chat_id] = route

    logger.info("Userbot aktif, listening %d pasangan source -> target ...", len(ROUTES))
    await alert_admin(f"Bot aktif, listening {len(ROUTES)} pasangan channel.")
    try:
        await client.run_until_disconnected()
    except Exception as e:
        logger.exception("Userbot berhenti karena error tak terduga.")
        await alert_admin(f"Bot CRASH dan berhenti: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
