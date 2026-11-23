"""Notify only the owner about captured notices; cannot confirm an order."""
import asyncio
import logging
import os
from pathlib import Path
import sqlite3

import aiohttp

from drivemods_bot.api import make_bot


def notice_text(event):
    if event['kind'] == 'transport_test':
        details = 'Техническая проверка соединения. Это не банковский платёж.'
    else:
        details = (f"В уведомлении MBANK: {event['amount_minor'] / 100:.2f} {event['currency']}.\n"
                   'Номера банковской операции в этом формате нет.')
    return (f"🧪 Уведомление принято сервером · тест №{event['seq']}\n{details}\n"
            'Режим проверки связи: оплата заказа не подтверждена, лицензия не приобретается.')


async def main():
    os.umask(0o077)
    owner_id = int(os.environ['BANK_NOTICE_OWNER_ID'])
    if str(owner_id) not in os.environ.get('ADMIN_IDS', '').split(','):
        raise ValueError('The notice recipient must be an existing bot administrator')
    token = os.environ['BOT_TOKEN']
    if token.split(':')[0] != '9227738530':
        raise ValueError('This forwarder is restricted to the existing license bot')
    path = Path(os.environ['BANK_NOTICE_FORWARD_DB'])
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY CHECK(id=1), seq INTEGER NOT NULL)')
    con.execute('INSERT OR IGNORE INTO cursor(id,seq) VALUES(1,0)')
    con.commit()
    headers = {'Authorization': 'Bearer ' + os.environ['BANK_NOTICE_READ_TOKEN']}
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as http, make_bot(token) as bot:
        me = await bot.get_me()
        if me.id != 9227738530:
            raise ValueError('Unexpected Telegram bot identity')
        while True:
            try:
                cursor = con.execute('SELECT seq FROM cursor WHERE id=1').fetchone()[0]
                async with http.get('http://127.0.0.1:8790/internal/events',
                                    params={'after': cursor}, headers=headers) as response:
                    response.raise_for_status()
                    body = await response.json()
                for event in body['events']:
                    # This is an informational owner notice, never a payment event.
                    # If Telegram loses its response, the same test number may reappear.
                    await bot.send_message(owner_id, notice_text(event), parse_mode=None)
                    con.execute('UPDATE cursor SET seq=? WHERE id=1', (event['seq'],))
                    con.commit()
            except Exception as exc:
                logging.warning('Bank notice forwarding deferred: %s', type(exc).__name__)
            await asyncio.sleep(3)


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
