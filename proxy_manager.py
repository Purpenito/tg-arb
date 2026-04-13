import logging
from urllib.parse import urlparse

from telegram import Bot
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest

logger = logging.getLogger(__name__)

NETWORK_ERROR_TOKENS = ("connect", "timeout", "network", "proxy", "socks", "dns", "reset")


def parse_proxy_url(proxy_url: str) -> dict:
    parsed = urlparse(proxy_url)
    if parsed.scheme not in ("socks5", "http", "https"):
        raise ValueError(f"Unsupported proxy scheme: {parsed.scheme}")
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "username": parsed.username,
        "password": parsed.password,
    }


class TelegramProxyManager:
    def __init__(self, token: str, proxy_list: list[str]):
        self.token = token
        self.proxy_list = proxy_list
        self.current_proxy_idx = -1
        self.bot: Bot | None = None

    @property
    def current_proxy(self) -> str | None:
        if self.current_proxy_idx < 0 or self.current_proxy_idx >= len(self.proxy_list):
            return None
        return self.proxy_list[self.current_proxy_idx]

    def _build_bot(self, proxy_url: str | None = None) -> Bot:
        if proxy_url:
            parse_proxy_url(proxy_url)
            request = HTTPXRequest(proxy=proxy_url)
            return Bot(token=self.token, request=request)
        return Bot(token=self.token, request=HTTPXRequest())

    async def _test_bot(self, bot: Bot) -> None:
        me = await bot.get_me()
        logger.info("Telegram connected as @%s", me.username)

    async def initialize(self) -> None:
        if not self.proxy_list:
            logger.info("Proxy list is empty; using direct Telegram connection")
            self.bot = self._build_bot()
            await self._test_bot(self.bot)
            return

        for idx, proxy_url in enumerate(self.proxy_list):
            masked = self._mask_proxy(proxy_url)
            logger.info("Trying Telegram proxy [%s/%s] %s", idx + 1, len(self.proxy_list), masked)
            try:
                bot = self._build_bot(proxy_url)
                await self._test_bot(bot)
                self.bot = bot
                self.current_proxy_idx = idx
                logger.info("Using Telegram proxy: %s", masked)
                return
            except Exception as exc:
                logger.warning("Proxy failed: %s (%s)", masked, exc)

        raise ConnectionError("All Telegram proxies failed")

    async def _switch_proxy(self) -> bool:
        if not self.proxy_list:
            return False

        start = self.current_proxy_idx if self.current_proxy_idx >= 0 else 0
        for offset in range(1, len(self.proxy_list) + 1):
            idx = (start + offset) % len(self.proxy_list)
            proxy_url = self.proxy_list[idx]
            masked = self._mask_proxy(proxy_url)
            logger.info("Switching Telegram proxy to %s", masked)
            try:
                bot = self._build_bot(proxy_url)
                await self._test_bot(bot)
                self.bot = bot
                self.current_proxy_idx = idx
                logger.info("Proxy switched successfully")
                return True
            except Exception as exc:
                logger.warning("Switch proxy failed: %s (%s)", masked, exc)
        return False

    async def send_message(self, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
        if self.bot is None:
            raise RuntimeError("Telegram bot is not initialized")

        attempts = max(1, len(self.proxy_list) + 1)
        for attempt in range(1, attempts + 1):
            try:
                await self.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, disable_web_page_preview=True)
                return True
            except (TimedOut, NetworkError) as exc:
                logger.warning("Telegram network error (%s/%s): %s", attempt, attempts, exc)
                if await self._switch_proxy():
                    continue
                return False
            except Exception as exc:
                message = str(exc).lower()
                logger.warning("Telegram send error (%s/%s): %s", attempt, attempts, exc)
                if any(token in message for token in NETWORK_ERROR_TOKENS) and await self._switch_proxy():
                    continue
                return False
        return False

    @staticmethod
    def _mask_proxy(proxy_url: str) -> str:
        try:
            parsed = urlparse(proxy_url)
            if parsed.password:
                return proxy_url.replace(parsed.password, "***")
        except Exception:
            return proxy_url
        return proxy_url
