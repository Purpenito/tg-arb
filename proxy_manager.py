import logging
from urllib.parse import urlparse

from telegram import Bot
from telegram.request import HTTPXRequest

logger = logging.getLogger(__name__)


def parse_proxy_url(proxy_url: str):
    parsed = urlparse(proxy_url)
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
        self.current_proxy_idx = 0
        self.bot: Bot | None = None

    def _build_bot(self, proxy_url: str | None = None) -> Bot:
        if proxy_url is None:
            return Bot(token=self.token, request=HTTPXRequest())

        parsed = parse_proxy_url(proxy_url)
        if parsed["scheme"] not in ("socks5", "http", "https"):
            raise ValueError(f"Неизвестный тип прокси: {parsed['scheme']}")
        return Bot(token=self.token, request=HTTPXRequest(proxy=proxy_url))

    async def _test_bot(self, bot: Bot) -> None:
        me = await bot.get_me()
        logger.info("Telegram бот подключён: @%s", me.username)

    async def initialize(self) -> None:
        if not self.proxy_list:
            logger.warning("Прокси не указаны — подключение напрямую")
            self.bot = self._build_bot(None)
            await self._test_bot(self.bot)
            return

        for index, proxy_url in enumerate(self.proxy_list, start=1):
            logger.info("Пробуем прокси [%s/%s]: %s", index, len(self.proxy_list), self._mask_proxy(proxy_url))
            try:
                candidate = self._build_bot(proxy_url)
                await self._test_bot(candidate)
                self.bot = candidate
                self.current_proxy_idx = index - 1
                logger.info("Прокси работает: %s", self._mask_proxy(proxy_url))
                return
            except Exception as exc:
                logger.warning("Прокси не работает: %s", exc)

        raise ConnectionError("Ни одно прокси не работает")

    async def _switch_proxy(self) -> bool:
        if not self.proxy_list:
            return False

        self.current_proxy_idx = (self.current_proxy_idx + 1) % len(self.proxy_list)
        proxy_url = self.proxy_list[self.current_proxy_idx]
        logger.info("Переключаемся на прокси: %s", self._mask_proxy(proxy_url))
        try:
            candidate = self._build_bot(proxy_url)
            await self._test_bot(candidate)
            self.bot = candidate
            logger.info("Новое прокси работает")
            return True
        except Exception as exc:
            logger.warning("Новое прокси не работает: %s", exc)
            return False

    async def send_message(self, chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
        if self.bot is None:
            raise RuntimeError("Telegram бот не инициализирован")

        attempts = max(len(self.proxy_list), 1)
        for attempt in range(1, attempts + 1):
            try:
                await self.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
                return True
            except Exception as exc:
                error = str(exc).lower()
                logger.warning("Ошибка отправки в Telegram (попытка %s/%s): %s", attempt, attempts, exc)
                if any(token in error for token in ("connect", "timeout", "network", "proxy", "socks")):
                    switched = await self._switch_proxy()
                    if switched:
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
            pass
        return proxy_url
