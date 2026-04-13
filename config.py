import os
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _parse_proxy_list(raw: str | None) -> List[str]:
    if not raw:
        return []
    items = [chunk.strip() for chunk in raw.replace(";", ",").split(",")]
    return [item for item in items if item]


# === TELEGRAM/SECRETS FROM ENV ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PROXY_LIST = _parse_proxy_list(os.getenv("PROXY_LIST"))

# === SETTINGS DEFAULTS ===
DEFAULT_MIN_PROFIT_PCT = 0.20
DEFAULT_MIN_VOLUME_USDT = 1000.0
DEFAULT_MIN_FUNDING_PCT = 0.03
DEFAULT_SYMBOLS = None  # None = all symbols, or e.g. ["BTC", "ETH", "SOL"]
DEFAULT_MODE = "all"

# === FEES (taker, decimal fraction) ===
FEES = {
    "bybit": {"spot": 0.0010, "futures": 0.00055},
    "bingx": {"spot": 0.0010, "futures": 0.00050},
    "kucoin": {"spot": 0.0010, "futures": 0.00060},
}

# === SCANNER LOOP ===
SCAN_INTERVAL = 8
UPDATE_PAIRS_INTERVAL = 1800
BATCH_SIZE = 20
HTTP_TIMEOUT_SECONDS = 10

# === LOGGING ===
DEFAULT_LOG_MAX_MB = 10
DEFAULT_LOG_BACKUPS = 5
PRESTART_MAX_LOG_MB = 100

# === COOLDOWN ===
COOLDOWN_SPREAD_SECONDS = 90
COOLDOWN_FUNDING_SECONDS = 300
