import os
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _parse_proxy_list(raw: str | None) -> List[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PROXY_LIST = _parse_proxy_list(os.getenv("PROXY_LIST"))

EXCHANGES = ["bybit", "kucoin", "okx", "bingx", "bitget", "gate"]

FEES = {
    "bybit": {"futures": 0.00055},
    "kucoin": {"futures": 0.00060},
    "okx": {"futures": 0.00050},
    "bingx": {"futures": 0.00050},
    "bitget": {"futures": 0.00060},
    "gate": {"futures": 0.00060},
}

DEFAULT_CAPITAL_USDT = 1000.0
DEFAULT_MIN_PROFIT_PCT = 0.15
DEFAULT_MIN_TRADE_SIZE_USDT = 100.0
DEFAULT_MIN_24H_VOLUME_USDT = 1_000_000.0
DEFAULT_MIN_FUNDING_PCT = 0.01
DEFAULT_MAX_NEGATIVE_ENTRY_SPREAD_PCT = 0.20
DEFAULT_FILTERED_SYMBOLS = None
DEFAULT_ENABLED_EXCHANGES = EXCHANGES.copy()
DEFAULT_MODE = "all"  # spread|funding|all
DEFAULT_AUTO_ALERTS_ENABLED = False
DEFAULT_COOLDOWN_MINUTES = 2
DEFAULT_MAX_RESULTS = 5
DEFAULT_ONLY_POSITIVE_SIGNALS = True
DEFAULT_FUNDING_WEIGHT = 0.5
DEFAULT_LOG_MAX_MB = 10
DEFAULT_LOG_BACKUPS = 5

SCAN_INTERVAL = 15
UPDATE_PAIRS_INTERVAL = 1800
BATCH_SIZE = 20
HTTP_TIMEOUT_SECONDS = 10
SUMMARY_EVERY_CYCLES = 10
PRESTART_MAX_LOG_MB = 120
DEPTH_LEVELS = 20
