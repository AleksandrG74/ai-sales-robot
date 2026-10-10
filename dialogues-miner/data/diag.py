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

    for name in ['newsbarcachat', 'chat_proefootball']:
       print(f'\n=== {name} ===')
        try:
            entity = await client.get_entity(name)
            print(f'  entity: {entity}')
            print(f'  id: {entity.id}')

            count = 0
            async for msg in client.iter_messages(entity, limit=10):
                count += 1
                print(f'  [{msg.date}] {msg.id}: {(msg.message or '')[:60]}')
            print(f'  total shown: {count}')
        except Exception as e:
            print(f'  ERROR: {type(e).__name__}: {e}')

    await client.disconnect()

asyncio.run(main())
