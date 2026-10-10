import asyncio
import os
from telethon import TelegramClient
from dotenv import load_dotenv

load_dotenv()

async def main():
    client = TelegramClient(
        '/app/sessions/telegram_session',
        int(os.getenv('TELEGRAM_API_ID')),
        os.getenv('TELEGRAM_API_HASH'),
    )
    await client.start(phone=os.getenv('TELEGRAM_PHONE'))
    print('CONNECTED')

    entity = await client.get_entity('@newsbarcachat')
    title = getattr(entity, "title", "N/A")
    print("title:", title)
    print("id:", entity.id)
    print("type:", type(entity).__name__)

    count = 0
    async for msg in client.iter_messages(entity, limit=5):
        count += 1
        text = (msg.message or "")[:50]
        print(f"  [{msg.date}] {text}")
    print("shown:", count)

    await client.disconnect()

asyncio.run(main())
