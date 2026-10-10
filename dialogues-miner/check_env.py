import os
from dotenv import load_dotenv
load_dotenv()
print('HASH:', repr(os.getenv('TELEGRAM_API_HASH')))
print('ID:', repr(os.getenv('TELEGRAM_API_ID')))

