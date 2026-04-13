# config.py

# === TELEGRAM ===
TELEGRAM_TOKEN = "API_TOKEN"
TELEGRAM_CHAT_ID = "6726197521"

# === ПРОКСИ (оставь пустым если не нужен) ===
PROXY_LIST = []

# === НАСТРОЙКИ ПО УМОЛЧАНИЮ ===
DEFAULT_MIN_PROFIT_PCT = 0.15
DEFAULT_MIN_VOLUME_USDT = 5000
DEFAULT_SYMBOLS = None  # None = все монеты; или ["BTC", "ETH"]

# === БИРЖИ ===
FEES = {
    "bybit": 0.001,
    "kucoin": 0.001,
    "bingx": 0.00045,
}

SCAN_INTERVAL = 8
UPDATE_PAIRS_INTERVAL = 1800
BATCH_SIZE = 20
