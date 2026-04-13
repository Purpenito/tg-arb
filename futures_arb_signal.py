import asyncio
from collections import Counter, defaultdict
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import config as cfg
from proxy_manager import TelegramProxyManager

SETTINGS_FILE = "settings.json"
LOG_PATH = Path("logs/arb_bot.log")
EXCHANGES = ("bybit", "bingx", "kucoin")

logger = logging.getLogger("arb_bot")


@dataclass
class MarketTop:
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float


@dataclass
class Opportunity:
    symbol: str
    mode: str
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    buy_qty: float
    sell_qty: float
    max_executable_size_usdt: float
    capital_usdt: float
    trade_size_usdt: float
    min_24h_volume_usdt: float
    gross_spread_pct: float
    fees_pct: float
    net_spread_pct: float
    funding_buy: Optional[float] = None
    funding_sell: Optional[float] = None
    net_funding_pct: float = 0.0
    total_edge_pct: float = 0.0
    estimated_spread_pnl_usdt: float = 0.0
    estimated_funding_pnl_usdt: float = 0.0
    estimated_total_pnl_usdt: float = 0.0
    buy_link: str = ""
    sell_link: str = ""


class Settings:
    def __init__(self) -> None:
        self.min_profit_pct = cfg.DEFAULT_MIN_PROFIT_PCT
        self.min_volume_usdt = cfg.DEFAULT_MIN_VOLUME_USDT
        self.min_funding_pct = cfg.DEFAULT_MIN_FUNDING_PCT
        self.min_24h_volume_usdt = cfg.DEFAULT_MIN_24H_VOLUME_USDT
        self.capital_usdt = cfg.DEFAULT_CAPITAL_USDT
        self.min_trade_size_usdt = cfg.DEFAULT_MIN_TRADE_SIZE_USDT
        self.max_negative_entry_spread_pct = cfg.DEFAULT_MAX_NEGATIVE_ENTRY_SPREAD_PCT
        self.enabled_exchanges = list(cfg.DEFAULT_ENABLED_EXCHANGES)
        self.auto_alerts_enabled = cfg.DEFAULT_AUTO_ALERTS_ENABLED
        self.cooldown_minutes = cfg.DEFAULT_COOLDOWN_MINUTES
        self.max_results = cfg.DEFAULT_MAX_RESULTS
        self.only_positive_signals = cfg.DEFAULT_ONLY_POSITIVE_SIGNALS
        self.funding_weight = cfg.DEFAULT_FUNDING_WEIGHT
        self.filtered_symbols: Optional[set[str]] = set(cfg.DEFAULT_SYMBOLS) if cfg.DEFAULT_SYMBOLS else None
        self.mode = cfg.DEFAULT_MODE
        self.log_max_mb = cfg.DEFAULT_LOG_MAX_MB
        self.log_backups = cfg.DEFAULT_LOG_BACKUPS

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_profit_pct": self.min_profit_pct,
            "min_volume_usdt": self.min_volume_usdt,
            "min_funding_pct": self.min_funding_pct,
            "min_24h_volume_usdt": self.min_24h_volume_usdt,
            "capital_usdt": self.capital_usdt,
            "min_trade_size_usdt": self.min_trade_size_usdt,
            "max_negative_entry_spread_pct": self.max_negative_entry_spread_pct,
            "enabled_exchanges": self.enabled_exchanges,
            "filtered_symbols": sorted(self.filtered_symbols) if self.filtered_symbols else None,
            "mode": self.mode,
            "auto_alerts_enabled": self.auto_alerts_enabled,
            "cooldown_minutes": self.cooldown_minutes,
            "max_results": self.max_results,
            "only_positive_signals": self.only_positive_signals,
            "funding_weight": self.funding_weight,
            "log_max_mb": self.log_max_mb,
            "log_backups": self.log_backups,
        }

    def from_dict(self, data: dict[str, Any]) -> None:
        self.min_profit_pct = float(data.get("min_profit_pct", cfg.DEFAULT_MIN_PROFIT_PCT))
        self.min_volume_usdt = float(data.get("min_volume_usdt", cfg.DEFAULT_MIN_VOLUME_USDT))
        self.min_funding_pct = float(data.get("min_funding_pct", cfg.DEFAULT_MIN_FUNDING_PCT))
        self.min_24h_volume_usdt = float(data.get("min_24h_volume_usdt", cfg.DEFAULT_MIN_24H_VOLUME_USDT))
        self.capital_usdt = float(data.get("capital_usdt", cfg.DEFAULT_CAPITAL_USDT))
        self.min_trade_size_usdt = float(data.get("min_trade_size_usdt", cfg.DEFAULT_MIN_TRADE_SIZE_USDT))
        self.max_negative_entry_spread_pct = float(data.get("max_negative_entry_spread_pct", cfg.DEFAULT_MAX_NEGATIVE_ENTRY_SPREAD_PCT))
        enabled_exchanges = data.get("enabled_exchanges", cfg.DEFAULT_ENABLED_EXCHANGES)
        self.enabled_exchanges = [str(ex).lower() for ex in enabled_exchanges if str(ex).lower() in EXCHANGES] or list(cfg.DEFAULT_ENABLED_EXCHANGES)
        syms = data.get("filtered_symbols")
        self.filtered_symbols = {s.upper() for s in syms} if syms else None
        self.mode = str(data.get("mode", cfg.DEFAULT_MODE)).lower()
        self.auto_alerts_enabled = bool(data.get("auto_alerts_enabled", cfg.DEFAULT_AUTO_ALERTS_ENABLED))
        self.cooldown_minutes = max(1, int(data.get("cooldown_minutes", cfg.DEFAULT_COOLDOWN_MINUTES)))
        self.max_results = max(1, int(data.get("max_results", cfg.DEFAULT_MAX_RESULTS)))
        self.only_positive_signals = bool(data.get("only_positive_signals", cfg.DEFAULT_ONLY_POSITIVE_SIGNALS))
        self.funding_weight = float(data.get("funding_weight", cfg.DEFAULT_FUNDING_WEIGHT))
        self.log_max_mb = max(1, int(data.get("log_max_mb", cfg.DEFAULT_LOG_MAX_MB)))
        self.log_backups = max(1, int(data.get("log_backups", cfg.DEFAULT_LOG_BACKUPS)))

    def reset_to_default(self) -> None:
        self.__init__()

    def save_to_file(self, filename: str) -> None:
        temp = f"{filename}.tmp"
        with open(temp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(temp, filename)

    def load_from_file(self, filename: str) -> None:
        if not os.path.exists(filename):
            self.save_to_file(filename)
            return
        try:
            with open(filename, "r", encoding="utf-8") as fh:
                self.from_dict(json.load(fh))
        except Exception as exc:
            logger.warning("Failed loading settings, using defaults: %s", exc)
            self.reset_to_default()
            self.save_to_file(filename)


settings = Settings()
last_signal_times: dict[str, float] = {}
latest_symbol_debug: dict[str, dict[str, Any]] = {}
latest_candidates: dict[str, Opportunity] = {}


class Diagnostics:
    def __init__(self) -> None:
        self.symbols_checked = 0
        self.signals_sent = 0
        self.funding_candidates = 0
        self.participation_count: Counter[str] = Counter()
        self.long_count: Counter[str] = Counter()
        self.short_count: Counter[str] = Counter()
        self.cut_reasons: dict[str, Counter[str]] = defaultdict(Counter)

    def reset(self) -> None:
        self.__init__()

    def cut(self, exchange: str, reason: str) -> None:
        self.cut_reasons[exchange][reason] += 1

    def summary(self, cycle_no: int) -> None:
        logger.info(
            "SUMMARY cycle=%s symbols_checked=%s signals_sent=%s funding_candidates=%s participation=%s long=%s short=%s cuts=%s",
            cycle_no,
            self.symbols_checked,
            self.signals_sent,
            self.funding_candidates,
            dict(self.participation_count),
            dict(self.long_count),
            dict(self.short_count),
            {ex: dict(counter) for ex, counter in self.cut_reasons.items()},
        )


def setup_logging(max_mb: int, backups: int) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > cfg.PRESTART_MAX_LOG_MB * 1024 * 1024:
        LOG_PATH.rename(LOG_PATH.with_suffix(".log.prestart"))

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    root.addHandler(sh)

    fh = RotatingFileHandler(LOG_PATH, maxBytes=max_mb * 1024 * 1024, backupCount=backups, encoding="utf-8")
    fh.setFormatter(formatter)
    root.addHandler(fh)
    fh.doRollover()


def get_mode_cooldown(mode: str) -> int:
    return int(settings.cooldown_minutes * 60)


def signal_key(symbol: str, mode: str, buy_exchange: str, sell_exchange: str) -> str:
    return f"{symbol}:{mode}:{buy_exchange}:{sell_exchange}"


def on_cooldown(symbol: str, mode: str, buy_exchange: str, sell_exchange: str) -> bool:
    key = signal_key(symbol, mode, buy_exchange, sell_exchange)
    now = time.time()
    last = last_signal_times.get(key, 0)
    if now - last < get_mode_cooldown(mode):
        return True
    last_signal_times[key] = now
    return False


def market_link(exchange: str, symbol: str, market_type: str) -> str:
    if exchange == "bybit":
        if market_type == "spot":
            return f"https://www.bybit.com/trade/spot/{symbol}USDT"
        return f"https://www.bybit.com/trade/usdt/{symbol}USDT"
    if exchange == "bingx":
        if market_type == "spot":
            return f"https://bingx.com/en/spot/{symbol}USDT"
        return f"https://bingx.com/en-us/futures/forward/{symbol}USDT"
    if exchange == "kucoin":
        if market_type == "spot":
            return f"https://www.kucoin.com/trade/{symbol}-USDT"
        return f"https://www.kucoin.com/futures/trade/{symbol}USDTM"
    return ""


def get_fee(exchange: str, market_type: str) -> float:
    return cfg.FEES.get(exchange, {}).get(market_type, 0.001)


def ts_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def funding_text(rate: Optional[float], side: str) -> str:
    if rate is None:
        return "N/A"
    pct = abs(rate) * 100
    side = side.upper()

    if side not in {"LONG", "SHORT"}:
        return f"{side} funding {rate * 100:.4f}%"

    if rate > 0:
        # Positive funding: LONG pays SHORT.
        action = "pays" if side == "LONG" else "receives"
    elif rate < 0:
        # Negative funding: SHORT pays LONG.
        action = "receives" if side == "LONG" else "pays"
    else:
        return f"{side} funding 0.0000%"

    return f"{side} {action} {pct:.4f}%"


async def fetch_json(session: aiohttp.ClientSession, url: str) -> Optional[dict[str, Any]]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=cfg.HTTP_TIMEOUT_SECONDS)) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None


async def fetch_json_with_reason(session: aiohttp.ClientSession, url: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=cfg.HTTP_TIMEOUT_SECONDS)) as resp:
            if resp.status == 429:
                return None, "rate limit"
            if resp.status != 200:
                return None, f"http {resp.status}"
            return await resp.json(), None
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception:
        return None, "network error"


async def fetch_symbol_maps(session: aiohttp.ClientSession) -> dict[str, dict[str, set[str]]]:
    maps = {"spot": {ex: set() for ex in EXCHANGES}, "futures": {ex: set() for ex in EXCHANGES}}

    bybit_f = await fetch_json(session, "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000")
    if bybit_f and bybit_f.get("retCode") == 0:
        for item in bybit_f["result"]["list"]:
            if item.get("quoteCoin") == "USDT" and item.get("status") == "Trading":
                maps["futures"]["bybit"].add(item["baseCoin"])

    bybit_s = await fetch_json(session, "https://api.bybit.com/v5/market/instruments-info?category=spot&limit=1000")
    if bybit_s and bybit_s.get("retCode") == 0:
        for item in bybit_s["result"]["list"]:
            if item.get("quoteCoin") == "USDT" and item.get("status") == "Trading":
                maps["spot"]["bybit"].add(item["baseCoin"])

    bingx_f = await fetch_json(session, "https://open-api.bingx.com/openApi/swap/v2/quote/contracts")
    if bingx_f and bingx_f.get("code") == "0":
        for item in bingx_f.get("data", []):
            symbol = item.get("symbol", "")
            if symbol.endswith("-USDT") and item.get("status") == 1:
                maps["futures"]["bingx"].add(symbol.replace("-USDT", ""))

    bingx_s = await fetch_json(session, "https://open-api.bingx.com/openApi/spot/v1/common/symbols")
    if bingx_s and int(bingx_s.get("code", 1)) == 0:
        for item in bingx_s.get("data", {}).get("symbols", []):
            symbol = item.get("symbol", "")
            if symbol.endswith("-USDT") and item.get("status") in (1, "1", "TRADING"):
                maps["spot"]["bingx"].add(symbol.replace("-USDT", ""))

    ku_f = await fetch_json(session, "https://api-futures.kucoin.com/api/v1/contracts/active")
    if ku_f and ku_f.get("code") == "200000":
        for item in ku_f.get("data", []):
            if item.get("settleCurrency") == "USDT" and item.get("status") == "Open":
                maps["futures"]["kucoin"].add(item.get("baseCurrency"))

    ku_s = await fetch_json(session, "https://api.kucoin.com/api/v2/symbols")
    if ku_s and ku_s.get("code") == "200000":
        for item in ku_s.get("data", []):
            if item.get("quoteCurrency") == "USDT" and item.get("enableTrading"):
                maps["spot"]["kucoin"].add(item.get("baseCurrency"))

    return maps


async def fetch_futures_book(session: aiohttp.ClientSession, exchange: str, symbol: str) -> tuple[Optional[MarketTop], Optional[str]]:
    if exchange == "bybit":
        data, reason = await fetch_json_with_reason(session, f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={symbol}USDT&limit=1")
        if reason:
            return None, reason
        if data and data.get("retCode") == 0 and data["result"].get("a") and data["result"].get("b"):
            return MarketTop(float(data["result"]["b"][0][0]), float(data["result"]["a"][0][0]), float(data["result"]["b"][0][1]), float(data["result"]["a"][0][1])), None
    elif exchange == "bingx":
        data, reason = await fetch_json_with_reason(session, f"https://open-api.bingx.com/openApi/swap/v2/quote/bookTicker?symbol={symbol}-USDT")
        if reason:
            return None, reason
        top = (data or {}).get("data", {}).get("book_ticker")
        if data and data.get("code") == "0" and top:
            return MarketTop(float(top["bid_price"]), float(top["ask_price"]), float(top["bid_qty"]), float(top["ask_qty"])), None
    elif exchange == "kucoin":
        data, reason = await fetch_json_with_reason(session, f"https://api-futures.kucoin.com/api/v1/level2/snapshot?symbol={symbol}USDTM")
        if reason:
            return None, reason
        if data and data.get("code") == "200000":
            bids = data["data"].get("bids") or []
            asks = data["data"].get("asks") or []
            if bids and asks:
                return MarketTop(float(bids[0][0]), float(asks[0][0]), float(bids[0][1]), float(asks[0][1])), None
    return None, "no orderbook"


async def fetch_spot_book(session: aiohttp.ClientSession, exchange: str, symbol: str) -> tuple[Optional[MarketTop], Optional[str]]:
    if exchange == "bybit":
        data, reason = await fetch_json_with_reason(session, f"https://api.bybit.com/v5/market/orderbook?category=spot&symbol={symbol}USDT&limit=1")
        if reason:
            return None, reason
        if data and data.get("retCode") == 0 and data["result"].get("a") and data["result"].get("b"):
            return MarketTop(float(data["result"]["b"][0][0]), float(data["result"]["a"][0][0]), float(data["result"]["b"][0][1]), float(data["result"]["a"][0][1])), None
    elif exchange == "bingx":
        data, reason = await fetch_json_with_reason(session, f"https://open-api.bingx.com/openApi/spot/v1/ticker/bookTicker?symbol={symbol}-USDT")
        if reason:
            return None, reason
        if data and int(data.get("code", 1)) == 0 and data.get("data"):
            top = data["data"]
            return MarketTop(float(top["bidPrice"]), float(top["askPrice"]), float(top.get("bidQty", 0)), float(top.get("askQty", 0))), None
    elif exchange == "kucoin":
        data, reason = await fetch_json_with_reason(session, f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}-USDT")
        if reason:
            return None, reason
        if data and data.get("code") == "200000" and data.get("data"):
            top = data["data"]
            return MarketTop(float(top["bestBid"]), float(top["bestAsk"]), float(top.get("bestBidSize", 0)), float(top.get("bestAskSize", 0))), None
    return None, "no orderbook"


async def fetch_funding(session: aiohttp.ClientSession, exchange: str, symbol: str) -> Optional[float]:
    if exchange == "bybit":
        data = await fetch_json(session, f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={symbol}USDT")
        if data and data.get("retCode") == 0 and data["result"]["list"]:
            return float(data["result"]["list"][0].get("fundingRate", 0))
    elif exchange == "bingx":
        data = await fetch_json(session, f"https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex?symbol={symbol}-USDT")
        if data and data.get("code") == "0" and data.get("data"):
            return float(data["data"].get("lastFundingRate", 0))
    elif exchange == "kucoin":
        data = await fetch_json(session, f"https://api-futures.kucoin.com/api/v1/contracts/{symbol}USDTM")
        if data and data.get("code") == "200000" and data.get("data"):
            return float(data["data"].get("fundingFeeRate", 0))
    return None


async def fetch_24h_volume(session: aiohttp.ClientSession, exchange: str, symbol: str, market_type: str) -> Optional[float]:
    if exchange == "bybit":
        category = "spot" if market_type == "spot" else "linear"
        data = await fetch_json(session, f"https://api.bybit.com/v5/market/tickers?category={category}&symbol={symbol}USDT")
        if data and data.get("retCode") == 0 and data["result"]["list"]:
            item = data["result"]["list"][0]
            if market_type == "spot":
                return float(item.get("turnover24h", 0) or 0)
            return float(item.get("turnover24h", 0) or 0)
    elif exchange == "bingx":
        if market_type == "spot":
            data = await fetch_json(session, f"https://open-api.bingx.com/openApi/spot/v1/ticker/24hr?symbol={symbol}-USDT")
            if data and int(data.get("code", 1)) == 0 and data.get("data"):
                return float(data["data"].get("quoteVolume", 0) or 0)
        else:
            data = await fetch_json(session, f"https://open-api.bingx.com/openApi/swap/v2/quote/ticker?symbol={symbol}-USDT")
            if data and data.get("code") == "0" and data.get("data"):
                return float(data["data"].get("quoteVolume", 0) or data["data"].get("amount", 0) or 0)
    elif exchange == "kucoin":
        if market_type == "spot":
            data = await fetch_json(session, f"https://api.kucoin.com/api/v1/market/stats?symbol={symbol}-USDT")
            if data and data.get("code") == "200000" and data.get("data"):
                return float(data["data"].get("volValue", 0) or 0)
        else:
            data = await fetch_json(session, f"https://api-futures.kucoin.com/api/v1/contracts/{symbol}USDTM")
            if data and data.get("code") == "200000" and data.get("data"):
                return float(data["data"].get("turnoverOf24h", 0) or 0)
    return None


def calc_opportunity(
    symbol: str,
    mode: str,
    buy_exchange: str,
    sell_exchange: str,
    buy_book: MarketTop,
    sell_book: MarketTop,
    buy_market: str,
    sell_market: str,
    buy_24h_volume_usdt: float,
    sell_24h_volume_usdt: float,
    capital_usdt: float,
    funding_buy: Optional[float] = None,
    funding_sell: Optional[float] = None,
) -> Opportunity:
    gross = ((sell_book.bid - buy_book.ask) / buy_book.ask) * 100
    fees_pct = (get_fee(buy_exchange, buy_market) + get_fee(sell_exchange, sell_market)) * 100
    net_spread_pct = gross - fees_pct
    max_size = min(buy_book.ask * buy_book.ask_qty, sell_book.bid * sell_book.bid_qty)
    trade_size = min(capital_usdt, max_size)
    min_24h_volume = min(buy_24h_volume_usdt, sell_24h_volume_usdt)
    net_funding_pct = 0.0
    total_edge_pct = net_spread_pct
    if funding_buy is not None and funding_sell is not None:
        net_funding_pct = (funding_sell - funding_buy) * 100
        total_edge_pct = net_spread_pct + (net_funding_pct * settings.funding_weight)

    return Opportunity(
        symbol=symbol,
        mode=mode,
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        buy_price=buy_book.ask,
        sell_price=sell_book.bid,
        buy_qty=buy_book.ask_qty,
        sell_qty=sell_book.bid_qty,
        max_executable_size_usdt=max_size,
        capital_usdt=capital_usdt,
        trade_size_usdt=trade_size,
        min_24h_volume_usdt=min_24h_volume,
        gross_spread_pct=gross,
        fees_pct=fees_pct,
        net_spread_pct=net_spread_pct,
        funding_buy=funding_buy,
        funding_sell=funding_sell,
        net_funding_pct=net_funding_pct,
        total_edge_pct=total_edge_pct,
        estimated_spread_pnl_usdt=trade_size * (net_spread_pct / 100),
        estimated_funding_pnl_usdt=trade_size * (net_funding_pct / 100),
        estimated_total_pnl_usdt=trade_size * (total_edge_pct / 100),
        buy_link=market_link(buy_exchange, symbol, buy_market),
        sell_link=market_link(sell_exchange, symbol, sell_market),
    )


def format_signal(opp: Opportunity) -> str:
    return (
        f"💎 <b>ARBITRAGE SIGNAL</b>\n"
        f"Coin: <b>{opp.symbol}/USDT</b>\n"
        f"Type: <code>{opp.mode}</code>\n\n"
        f"🟢 BUY/LONG: <b>{opp.buy_exchange.upper()}</b>\n"
        f"Price: <code>{opp.buy_price:.6f}</code> | Qty: <code>{opp.buy_qty:.4f}</code>\n"
        f"Link: {opp.buy_link}\n\n"
        f"🔴 SELL/SHORT: <b>{opp.sell_exchange.upper()}</b>\n"
        f"Price: <code>{opp.sell_price:.6f}</code> | Qty: <code>{opp.sell_qty:.4f}</code>\n"
        f"Link: {opp.sell_link}\n\n"
        f"Capital: <code>{opp.capital_usdt:,.2f} USDT</code>\n"
        f"24h volume (min side): <code>{opp.min_24h_volume_usdt:,.2f} USDT</code>\n"
        f"Max executable size: <code>{opp.max_executable_size_usdt:,.2f} USDT</code>\n"
        f"Trade size: <code>{opp.trade_size_usdt:,.2f} USDT</code>\n"
        f"Gross spread: <code>{opp.gross_spread_pct:.4f}%</code>\n"
        f"Fees: <code>{opp.fees_pct:.4f}%</code>\n"
        f"Spread edge: <code>{opp.net_spread_pct:+.4f}%</code>\n"
        f"Funding buy/sell: <code>{'N/A' if opp.funding_buy is None else f'{opp.funding_buy*100:.4f}%'} / {'N/A' if opp.funding_sell is None else f'{opp.funding_sell*100:.4f}%'}</code>\n"
        f"Funding explain: <code>{funding_text(opp.funding_buy, 'LONG')} | {funding_text(opp.funding_sell, 'SHORT')}</code>\n"
        f"Funding edge: <code>{opp.net_funding_pct:+.4f}%</code>\n"
        f"Total edge: <b>{opp.total_edge_pct:+.4f}%</b>\n"
        f"Estimated spread PnL: <code>{opp.estimated_spread_pnl_usdt:,.2f} USDT</code>\n"
        f"Estimated funding PnL: <code>{opp.estimated_funding_pnl_usdt:,.2f} USDT</code>\n"
        f"Estimated total PnL: <b>{opp.estimated_total_pnl_usdt:,.2f} USDT</b>\n"
        f"Time: {ts_utc()}"
    )


async def scan_symbol(
    session: aiohttp.ClientSession,
    tg: TelegramProxyManager,
    symbol: str,
    maps: dict[str, dict[str, set[str]]],
    diagnostics: Diagnostics,
) -> None:
    diagnostics.symbols_checked += 1
    allowed_exchanges = set(settings.enabled_exchanges)
    futures_ex = [ex for ex in EXCHANGES if ex in allowed_exchanges and symbol in maps["futures"][ex]]
    spot_ex = [ex for ex in EXCHANGES if ex in allowed_exchanges and symbol in maps["spot"][ex]]
    debug_info: dict[str, Any] = {
        "symbol": symbol,
        "futures_exchanges": futures_ex,
        "spot_exchanges": spot_ex,
        "cuts": [],
        "best_candidate": None,
    }
    for exchange in EXCHANGES:
        if exchange not in allowed_exchanges:
            diagnostics.cut(exchange, "exchange disabled")
        elif exchange not in futures_ex and exchange not in spot_ex:
            diagnostics.cut(exchange, "no symbol")

    opportunities: list[Opportunity] = []

    async def get_side_24h(exchange: str, market: str) -> float:
        value = await fetch_24h_volume(session, exchange, symbol, market)
        return value if value is not None else 0.0

    if settings.mode in ("all", "spread", "futures_futures", "funding") and len(futures_ex) >= 2:
        results = await asyncio.gather(*[fetch_futures_book(session, ex, symbol) for ex in futures_ex])
        books: dict[str, MarketTop] = {}
        for ex, (book, reason) in zip(futures_ex, results):
            if book:
                books[ex] = book
                diagnostics.participation_count[ex] += 1
            else:
                diagnostics.cut(ex, reason or "no orderbook")
                debug_info["cuts"].append(f"{ex}: {reason or 'no orderbook'}")
        logger.info("ORDERBOOK futures symbol=%s available=%s", symbol, sorted(books.keys()))
        if len(books) >= 2:
            long_ex = min(books, key=lambda x: books[x].ask)
            short_ex = max(books, key=lambda x: books[x].bid)
            if long_ex != short_ex:
                if settings.mode in ("all", "spread", "futures_futures"):
                    long_vol_24h, short_vol_24h = await asyncio.gather(
                        get_side_24h(long_ex, "futures"),
                        get_side_24h(short_ex, "futures"),
                    )
                    opportunities.append(
                        calc_opportunity(
                            symbol,
                            "futures_futures",
                            long_ex,
                            short_ex,
                            books[long_ex],
                            books[short_ex],
                            "futures",
                            "futures",
                            long_vol_24h,
                            short_vol_24h,
                            settings.capital_usdt,
                        )
                    )
                if settings.mode in ("all", "funding"):
                    f_long, f_short, long_vol_24h, short_vol_24h = await asyncio.gather(
                        fetch_funding(session, long_ex, symbol),
                        fetch_funding(session, short_ex, symbol),
                        get_side_24h(long_ex, "futures"),
                        get_side_24h(short_ex, "futures"),
                    )
                    opportunities.append(
                        calc_opportunity(
                            symbol,
                            "funding",
                            long_ex,
                            short_ex,
                            books[long_ex],
                            books[short_ex],
                            "futures",
                            "futures",
                            long_vol_24h,
                            short_vol_24h,
                            settings.capital_usdt,
                            f_long,
                            f_short,
                        )
                    )

    if settings.mode in ("all", "spread", "spot_futures") and spot_ex and futures_ex:
        spot_results = await asyncio.gather(*[fetch_spot_book(session, ex, symbol) for ex in spot_ex])
        fut_results = await asyncio.gather(*[fetch_futures_book(session, ex, symbol) for ex in futures_ex])
        spot_books: dict[str, MarketTop] = {}
        fut_books: dict[str, MarketTop] = {}
        for ex, (book, reason) in zip(spot_ex, spot_results):
            if book:
                spot_books[ex] = book
                diagnostics.participation_count[ex] += 1
            else:
                diagnostics.cut(ex, reason or "no orderbook")
                debug_info["cuts"].append(f"{ex}: {reason or 'no orderbook'}")
        for ex, (book, reason) in zip(futures_ex, fut_results):
            if book:
                fut_books[ex] = book
                diagnostics.participation_count[ex] += 1
            else:
                diagnostics.cut(ex, reason or "no orderbook")
                debug_info["cuts"].append(f"{ex}: {reason or 'no orderbook'}")
        logger.info("ORDERBOOK spot symbol=%s available=%s | futures available=%s", symbol, sorted(spot_books.keys()), sorted(fut_books.keys()))
        if spot_books and fut_books:
            buy_ex = min(spot_books, key=lambda x: spot_books[x].ask)
            sell_ex = max(fut_books, key=lambda x: fut_books[x].bid)
            buy_vol_24h, sell_vol_24h = await asyncio.gather(
                get_side_24h(buy_ex, "spot"),
                get_side_24h(sell_ex, "futures"),
            )
            opportunities.append(
                calc_opportunity(
                    symbol,
                    "spot_futures",
                    buy_ex,
                    sell_ex,
                    spot_books[buy_ex],
                    fut_books[sell_ex],
                    "spot",
                    "futures",
                    buy_vol_24h,
                    sell_vol_24h,
                    settings.capital_usdt,
                )
            )

    for opp in opportunities:
        if opp.max_executable_size_usdt < settings.min_volume_usdt:
            diagnostics.cut(opp.buy_exchange, f"low top-of-book volume got={opp.max_executable_size_usdt:.2f} required={settings.min_volume_usdt:.2f}")
            diagnostics.cut(opp.sell_exchange, f"low top-of-book volume got={opp.max_executable_size_usdt:.2f} required={settings.min_volume_usdt:.2f}")
            continue
        if opp.trade_size_usdt < settings.min_trade_size_usdt:
            diagnostics.cut(opp.buy_exchange, f"low trade_size_usdt got={opp.trade_size_usdt:.2f} required={settings.min_trade_size_usdt:.2f}")
            diagnostics.cut(opp.sell_exchange, f"low trade_size_usdt got={opp.trade_size_usdt:.2f} required={settings.min_trade_size_usdt:.2f}")
            continue
        if opp.min_24h_volume_usdt < settings.min_24h_volume_usdt:
            diagnostics.cut(opp.buy_exchange, f"low 24h volume got={opp.min_24h_volume_usdt:.2f} required={settings.min_24h_volume_usdt:.2f}")
            diagnostics.cut(opp.sell_exchange, f"low 24h volume got={opp.min_24h_volume_usdt:.2f} required={settings.min_24h_volume_usdt:.2f}")
            continue
        if opp.net_spread_pct < 0:
            diagnostics.cut(opp.buy_exchange, f"negative spread after fees gross={opp.gross_spread_pct:.4f} fees={opp.fees_pct:.4f} net={opp.net_spread_pct:.4f}")
            diagnostics.cut(opp.sell_exchange, f"negative spread after fees gross={opp.gross_spread_pct:.4f} fees={opp.fees_pct:.4f} net={opp.net_spread_pct:.4f}")
            continue
        if opp.mode == "funding":
            diagnostics.funding_candidates += 1
            if opp.net_spread_pct < -settings.max_negative_entry_spread_pct:
                diagnostics.cut(opp.buy_exchange, f"bad funding edge entry spread={opp.net_spread_pct:.4f} limit=-{settings.max_negative_entry_spread_pct:.4f}")
                diagnostics.cut(opp.sell_exchange, f"bad funding edge entry spread={opp.net_spread_pct:.4f} limit=-{settings.max_negative_entry_spread_pct:.4f}")
                continue
            if opp.net_funding_pct < settings.min_funding_pct:
                diagnostics.cut(opp.buy_exchange, f"bad funding edge got={opp.net_funding_pct:.4f} required={settings.min_funding_pct:.4f}")
                diagnostics.cut(opp.sell_exchange, f"bad funding edge got={opp.net_funding_pct:.4f} required={settings.min_funding_pct:.4f}")
                continue
            if opp.total_edge_pct < settings.min_profit_pct:
                diagnostics.cut(opp.buy_exchange, f"bad total edge got={opp.total_edge_pct:.4f} required={settings.min_profit_pct:.4f}")
                diagnostics.cut(opp.sell_exchange, f"bad total edge got={opp.total_edge_pct:.4f} required={settings.min_profit_pct:.4f}")
                continue
        else:
            if opp.net_spread_pct < settings.min_profit_pct:
                diagnostics.cut(opp.buy_exchange, f"bad spread edge got={opp.net_spread_pct:.4f} required={settings.min_profit_pct:.4f}")
                diagnostics.cut(opp.sell_exchange, f"bad spread edge got={opp.net_spread_pct:.4f} required={settings.min_profit_pct:.4f}")
                continue
        if settings.only_positive_signals and opp.estimated_total_pnl_usdt <= 0:
            diagnostics.cut(opp.buy_exchange, f"only positive signals pnl={opp.estimated_total_pnl_usdt:.4f}")
            diagnostics.cut(opp.sell_exchange, f"only positive signals pnl={opp.estimated_total_pnl_usdt:.4f}")
            continue

        if on_cooldown(opp.symbol, opp.mode, opp.buy_exchange, opp.sell_exchange):
            continue

        prev = latest_candidates.get(opp.symbol)
        if prev is None or opp.total_edge_pct > prev.total_edge_pct:
            latest_candidates[opp.symbol] = opp

        debug_info["best_candidate"] = {
            "mode": opp.mode,
            "buy_exchange": opp.buy_exchange,
            "sell_exchange": opp.sell_exchange,
            "net_spread_pct": opp.net_spread_pct,
            "net_funding_pct": opp.net_funding_pct,
            "total_edge_pct": opp.total_edge_pct,
            "trade_size_usdt": opp.trade_size_usdt,
        }
        diagnostics.long_count[opp.buy_exchange] += 1
        diagnostics.short_count[opp.sell_exchange] += 1
        diagnostics.signals_sent += 1
        latest_symbol_debug[symbol.upper()] = debug_info
        if settings.auto_alerts_enabled:
            await tg.send_message(cfg.TELEGRAM_CHAT_ID, format_signal(opp))
    if symbol.upper() not in latest_symbol_debug:
        latest_symbol_debug[symbol.upper()] = debug_info


async def start_telegram_bot() -> Application:
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env")

    allowed_chat_id = str(cfg.TELEGRAM_CHAT_ID)

    async def auth_check(update: Update) -> bool:
        chat_id = str(update.effective_chat.id) if update.effective_chat else ""
        message = update.effective_message
        if chat_id != allowed_chat_id:
            if message:
                await message.reply_text("🔒 Access denied")
            return False
        return True

    def main_menu() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Проверить всё", callback_data="scan_all"), InlineKeyboardButton("Только spread", callback_data="scan_spread")],
            [InlineKeyboardButton("Только funding", callback_data="scan_funding"), InlineKeyboardButton("Только spot-futures", callback_data="scan_spot_futures")],
            [InlineKeyboardButton("Уведомления ВКЛ/ВЫКЛ", callback_data="toggle_alerts"), InlineKeyboardButton("Статус", callback_data="status")],
            [InlineKeyboardButton("Помощь", callback_data="help")],
        ])

    async def send_top_signals(message, forced_mode: Optional[str] = None) -> None:
        if not latest_candidates:
            await message.reply_text("Сигналов по текущим фильтрам пока нет. Нажмите позже.")
            return
        filtered = list(latest_candidates.values())
        if forced_mode:
            if forced_mode == "spread":
                filtered = [o for o in filtered if o.mode in {"futures_futures", "spot_futures"}]
            else:
                filtered = [o for o in filtered if o.mode == forced_mode]
        filtered.sort(key=lambda x: x.total_edge_pct, reverse=True)
        limited = filtered[: settings.max_results]
        for opp in limited:
            await message.reply_text(format_signal(opp), parse_mode="HTML", disable_web_page_preview=True)

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        message = update.effective_message
        if not message:
            return
        await message.reply_text(
            "Bot online.\n"
            "Commands:\n"
            "/show\n"
            "/set min_profit 0.2\n"
            "/set min_volume 1000\n"
            "/set min_24h_volume 1000000\n"
            "/set min_trade_size 50\n"
            "/set capital 1000\n"
            "/set min_funding 0.03\n"
            "/set cooldown 2\n"
            "/set max_results 5\n"
            "/set alerts on|off\n"
            "/set symbols BTC,ETH,SOL\n"
            "/set symbols ALL\n"
            "/mode spread|funding|spot_futures|futures_futures|all\n"
            "/debug ETH\n"
            "/status\n"
            "/reset",
            parse_mode="HTML",
            reply_markup=main_menu(),
        )

    async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        message = update.effective_message
        if not message:
            return
        symbols = "ALL" if not settings.filtered_symbols else ",".join(sorted(settings.filtered_symbols))
        await message.reply_text(
            f"Режим: {settings.mode}\n"
            f"Мин. профит: {settings.min_profit_pct:.3f}%\n"
            f"Мин. исполнимый объём: ${settings.min_volume_usdt:,.0f} USDT\n"
            f"Мин. объём за 24ч: ${settings.min_24h_volume_usdt:,.0f} USDT\n"
            f"Мин. размер сделки: ${settings.min_trade_size_usdt:,.0f} USDT\n"
            f"Капитал: ${settings.capital_usdt:,.0f} USDT\n"
            f"Мин. funding: {settings.min_funding_pct:.3f}% (info only)\n"
            f"Funding weight: {settings.funding_weight:.2f}\n"
            f"Max negative entry spread (funding): {settings.max_negative_entry_spread_pct:.3f}%\n"
            f"Активные биржи: {', '.join(settings.enabled_exchanges)}\n"
            f"Автоуведомления: {'ВКЛ' if settings.auto_alerts_enabled else 'ВЫКЛ'}\n"
            f"Cooldown: {settings.cooldown_minutes} мин\n"
            f"Max результатов: {settings.max_results}\n"
            f"Символы: {symbols}\n"
            f"log_max_mb={settings.log_max_mb}\n"
            f"log_backups={settings.log_backups}"
        )

    async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        message = update.effective_message
        if not message:
            return
        settings.reset_to_default()
        settings.save_to_file(SETTINGS_FILE)
        await message.reply_text("Settings reset", reply_markup=main_menu())

    async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        message = update.effective_message
        if not message:
            return
        args = context.args
        if len(args) < 2:
            await message.reply_text(
                "Usage:\n"
                "/set min_profit 0.2\n"
                "/set min_volume 1000\n"
                "/set min_24h_volume 1000000\n"
                "/set min_trade_size 50\n"
                "/set capital 1000\n"
                "/set min_funding 0.03\n"
                "/set max_negative_entry_spread 0.2\n"
                "/set funding_weight 0.5\n"
                "/set exchanges bybit,bingx,kucoin\n"
                "/set alerts on\n"
                "/set cooldown 2\n"
                "/set max_results 5\n"
                "/set symbols BTC,ETH\n"
                "/set symbols ALL"
            )
            return
        key = args[0].lower()
        value = " ".join(args[1:]).strip()
        try:
            if key == "min_profit":
                settings.min_profit_pct = float(value)
            elif key == "min_volume":
                settings.min_volume_usdt = float(value)
            elif key == "min_funding":
                settings.min_funding_pct = float(value)
            elif key == "min_24h_volume":
                settings.min_24h_volume_usdt = float(value)
            elif key == "min_trade_size":
                settings.min_trade_size_usdt = float(value)
            elif key == "capital":
                settings.capital_usdt = float(value)
            elif key == "max_negative_entry_spread":
                settings.max_negative_entry_spread_pct = float(value)
            elif key == "funding_weight":
                settings.funding_weight = float(value)
            elif key == "cooldown":
                settings.cooldown_minutes = max(1, int(float(value)))
            elif key == "max_results":
                settings.max_results = max(1, int(float(value)))
            elif key == "alerts":
                settings.auto_alerts_enabled = value.lower() in {"on", "1", "true", "yes", "вкл"}
            elif key == "exchanges":
                items = [x.strip().lower() for x in value.split(",") if x.strip()]
                selected = [x for x in items if x in EXCHANGES]
                if selected:
                    settings.enabled_exchanges = selected
            elif key == "symbols":
                if value.upper() == "ALL":
                    settings.filtered_symbols = None
                else:
                    settings.filtered_symbols = {s.strip().upper() for s in value.split(",") if s.strip()}
            else:
                await message.reply_text("Unknown key")
                return
            settings.save_to_file(SETTINGS_FILE)
            await message.reply_text("Updated")
        except ValueError:
            await message.reply_text("Invalid value")

    async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        message = update.effective_message
        if not message:
            return
        if not context.args:
            await message.reply_text("Usage: /mode spread|funding|spot_futures|futures_futures|all")
            return
        mode = context.args[0].lower()
        allowed = {"spread", "funding", "spot_futures", "futures_futures", "all"}
        if mode not in allowed:
            await message.reply_text("Invalid mode")
            return
        settings.mode = mode
        settings.save_to_file(SETTINGS_FILE)
        await message.reply_text(f"Mode set to {mode}")

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        message = update.effective_message
        if not message:
            return
        await cmd_show(update, context)

    async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        message = update.effective_message
        if not message:
            return
        if not context.args:
            await message.reply_text("Usage: /debug ETH")
            return
        sym = context.args[0].upper()
        info = latest_symbol_debug.get(sym)
        if not info:
            await message.reply_text(f"Нет данных debug по {sym} пока.")
            return
        await message.reply_text(
            f"DEBUG {sym}\n"
            f"futures_exchanges={info.get('futures_exchanges')}\n"
            f"spot_exchanges={info.get('spot_exchanges')}\n"
            f"cuts={info.get('cuts')}\n"
            f"best_candidate={info.get('best_candidate')}"
        )

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        query = update.callback_query
        if not query:
            return
        await query.answer()
        if query.data == "toggle_alerts":
            settings.auto_alerts_enabled = not settings.auto_alerts_enabled
            settings.save_to_file(SETTINGS_FILE)
            await query.message.reply_text(f"Автоуведомления: {'ВКЛ' if settings.auto_alerts_enabled else 'ВЫКЛ'}")
            return
        if query.data == "status":
            await cmd_status(update, context)
            return
        if query.data == "help":
            await cmd_start(update, context)
            return
        mode_map = {
            "scan_all": None,
            "scan_spread": "spread",
            "scan_funding": "funding",
            "scan_spot_futures": "spot_futures",
        }
        if query.data in mode_map:
            await send_top_signals(query.message, mode_map[query.data])

    app_builder = Application.builder().token(cfg.TELEGRAM_TOKEN)
    if cfg.PROXY_LIST:
        app_builder = app_builder.proxy(cfg.PROXY_LIST[0]).get_updates_proxy(cfg.PROXY_LIST[0])
    app = app_builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CallbackQueryHandler(on_button))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    return app


async def main() -> None:
    settings.load_from_file(SETTINGS_FILE)
    setup_logging(settings.log_max_mb, settings.log_backups)

    tg = TelegramProxyManager(cfg.TELEGRAM_TOKEN, cfg.PROXY_LIST)
    await tg.initialize()
    app = await start_telegram_bot()

    connector = aiohttp.TCPConnector(limit=50)
    diagnostics = Diagnostics()
    cycle_no = 0
    async with aiohttp.ClientSession(connector=connector) as session:
        symbol_maps = await fetch_symbol_maps(session)
        last_update = time.time()

        while True:
            if time.time() - last_update > cfg.UPDATE_PAIRS_INTERVAL:
                symbol_maps = await fetch_symbol_maps(session)
                last_update = time.time()

            all_symbols = sorted(set().union(*symbol_maps["futures"].values(), *symbol_maps["spot"].values()))
            scan_symbols = [s for s in all_symbols if not settings.filtered_symbols or s in settings.filtered_symbols]

            cycle_no += 1
            tasks = [scan_symbol(session, tg, symbol, symbol_maps, diagnostics) for symbol in scan_symbols]
            for i in range(0, len(tasks), cfg.BATCH_SIZE):
                batch = tasks[i : i + cfg.BATCH_SIZE]
                await asyncio.gather(*batch, return_exceptions=True)
                await asyncio.sleep(0.3)

            if cycle_no % cfg.SUMMARY_EVERY_CYCLES == 0:
                diagnostics.summary(cycle_no)
                diagnostics.reset()

            await asyncio.sleep(cfg.SCAN_INTERVAL)

    await app.updater.stop()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
