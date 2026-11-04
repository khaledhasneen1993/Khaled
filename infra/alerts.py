import asyncio
import logging
import os
import httpx

logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)


class TelegramAlerter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        self.chat_id = os.getenv('TELEGRAM_CHAT_ID', '')
        self.enabled = bool(cfg['telegram_enabled'] and self.token and self.chat_id)
        self.queue = asyncio.Queue(maxsize=cfg['queue_size'])
        self.failed = False

    async def send(self, msg: str, level='info'):
        if not self.enabled:
            return False
        text = f'[{level}] {msg}'
        limit = self.cfg['message_max_chars']
        for offset in range(0, len(text), limit):
            await self.queue.put(text[offset:offset + limit])
        return True

    async def _worker(self):
        if not self.enabled:
            await asyncio.Future()  # cancellable; no HTTP client or network
            return
        async with httpx.AsyncClient(timeout=self.cfg['http_timeout_sec']) as client:
            while True:
                msg = await self.queue.get()
                delivered = False
                try:
                    for _ in range(self.cfg['max_attempts']):
                        try:
                            response = await client.post(
                                f"{self.cfg['base_url']}/bot{self.token}/sendMessage",
                                json={'chat_id': self.chat_id, 'text': msg})
                            delivered = response.status_code == 200 and response.json().get('ok') is True
                        except Exception:
                            delivered = False
                        if delivered:
                            break
                        await asyncio.sleep(self.cfg['retry_sec'])
                    self.failed = not delivered
                    if not delivered:
                        logging.getLogger('CASCADE').error('Telegram delivery failed; see durable operational journal')
                finally:
                    self.queue.task_done()
