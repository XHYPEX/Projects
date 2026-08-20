import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_PATH = "session/userbot"

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)


async def main():
    await client.start()
    print(f"{'ID':<16} {'Username':<25} Nama")
    print("-" * 70)
    async for dialog in client.iter_dialogs():
        username = f"@{dialog.entity.username}" if getattr(dialog.entity, "username", None) else "-"
        print(f"{dialog.id:<16} {username:<25} {dialog.name}")


if __name__ == "__main__":
    asyncio.run(main())
