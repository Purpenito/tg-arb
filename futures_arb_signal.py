import asyncio
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
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

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
            "filtered_symbols": sorted(self.filtered_symbols) if self.filtered_symbols else None,
            "mode": self.mode,
            "log_max_mb": self.log_max_mb,
            "log_backups": self.log_backups,
        }

    def from_dict(self, data: dict[str, Any]) -> None:
        self.min_profit_pct = float(data.get("min_profit_pct", cfg.DEFAULT_MIN_PROFIT_PCT))
        self.min_volume_usdt = float(data.get("min_volume_usdt", cfg.DEFAULT_MIN_VOLUME_USDT))
        self.min_funding_pct = float(data.get("min_funding_pct", cfg.DEFAULT_MIN_FUNDING_PCT))
        self.min_24h_volume_usdt = float(data.get("min_24h_volume_usdt", cfg.DEFAULT_MIN_24H_VOLUME_USDT))
        syms = data.get("filtered_symbols")
        self.filtered_symbols = {s.upper() for s in syms} if syms else None
        self.mode = str(data.get("mode", cfg.DEFAULT_MODE)).lower()
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
    return cfg.COOLDOWN_FUNDING_SECONDS if mode == "funding" else cfg.COOLDOWN_SPREAD_SECONDS


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


async def fetch_futures_book(session: aiohttp.ClientSession, exchange: str, symbol: str) -> Optional[MarketTop]:
    if exchange == "bybit":
        data = await fetch_json(session, f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={symbol}USDT&limit=1")
        if data and data.get("retCode") == 0 and data["result"].get("a") and data["result"].get("b"):
            return MarketTop(float(data["result"]["b"][0][0]), float(data["result"]["a"][0][0]), float(data["result"]["b"][0][1]), float(data["result"]["a"][0][1]))
    elif exchange == "bingx":
        data = await fetch_json(session, f"https://open-api.bingx.com/openApi/swap/v2/quote/bookTicker?symbol={symbol}-USDT")
        top = (data or {}).get("data", {}).get("book_ticker")
        if data and data.get("code") == "0" and top:
            return MarketTop(float(top["bid_price"]), float(top["ask_price"]), float(top["bid_qty"]), float(top["ask_qty"]))
    elif exchange == "kucoin":
        data = await fetch_json(session, f"https://api-futures.kucoin.com/api/v1/level2/snapshot?symbol={symbol}USDTM")
        if data and data.get("code") == "200000":
            bids = data["data"].get("bids") or []
            asks = data["data"].get("asks") or []
            if bids and asks:
                return MarketTop(float(bids[0][0]), float(asks[0][0]), float(bids[0][1]), float(asks[0][1]))
    return None


async def fetch_spot_book(session: aiohttp.ClientSession, exchange: str, symbol: str) -> Optional[MarketTop]:
    if exchange == "bybit":
        data = await fetch_json(session, f"https://api.bybit.com/v5/market/orderbook?category=spot&symbol={symbol}USDT&limit=1")
        if data and data.get("retCode") == 0 and data["result"].get("a") and data["result"].get("b"):
            return MarketTop(float(data["result"]["b"][0][0]), float(data["result"]["a"][0][0]), float(data["result"]["b"][0][1]), float(data["result"]["a"][0][1]))
    elif exchange == "bingx":
        data = await fetch_json(session, f"https://open-api.bingx.com/openApi/spot/v1/ticker/bookTicker?symbol={symbol}-USDT")
        if data and int(data.get("code", 1)) == 0 and data.get("data"):
            top = data["data"]
            return MarketTop(float(top["bidPrice"]), float(top["askPrice"]), float(top.get("bidQty", 0)), float(top.get("askQty", 0)))
    elif exchange == "kucoin":
        data = await fetch_json(session, f"https://api.kucoin.com/api/v1/market/orderbook/level1?symbol={symbol}-USDT")
        if data and data.get("code") == "200000" and data.get("data"):
            top = data["data"]
            return MarketTop(float(top["bestBid"]), float(top["bestAsk"]), float(top.get("bestBidSize", 0)), float(top.get("bestAskSize", 0)))
    return None


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
    funding_buy: Optional[float] = None,
    funding_sell: Optional[float] = None,
) -> Opportunity:
    gross = ((sell_book.bid - buy_book.ask) / buy_book.ask) * 100
    fees_pct = (get_fee(buy_exchange, buy_market) + get_fee(sell_exchange, sell_market)) * 100
    net_spread_pct = gross - fees_pct
    max_size = min(buy_book.ask * buy_book.ask_qty, sell_book.bid * sell_book.bid_qty)
    min_24h_volume = min(buy_24h_volume_usdt, sell_24h_volume_usdt)
    net_funding_pct = 0.0
    total_edge_pct = net_spread_pct
    if funding_buy is not None and funding_sell is not None:
        net_funding_pct = (funding_sell - funding_buy) * 100
        total_edge_pct = net_spread_pct + net_funding_pct

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
        min_24h_volume_usdt=min_24h_volume,
        gross_spread_pct=gross,
        fees_pct=fees_pct,
        net_spread_pct=net_spread_pct,
        funding_buy=funding_buy,
        funding_sell=funding_sell,
        net_funding_pct=net_funding_pct,
        total_edge_pct=total_edge_pct,
        estimated_spread_pnl_usdt=max_size * (net_spread_pct / 100),
        estimated_funding_pnl_usdt=max_size * (net_funding_pct / 100),
        estimated_total_pnl_usdt=max_size * (total_edge_pct / 100),
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
        f"24h volume (min side): <code>{opp.min_24h_volume_usdt:,.2f} USDT</code>\n"
        f"Max executable size: <code>{opp.max_executable_size_usdt:,.2f} USDT</code>\n"
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


async def scan_symbol(session: aiohttp.ClientSession, tg: TelegramProxyManager, symbol: str, maps: dict[str, dict[str, set[str]]]) -> None:
    futures_ex = [ex for ex in EXCHANGES if symbol in maps["futures"][ex]]
    spot_ex = [ex for ex in EXCHANGES if symbol in maps["spot"][ex]]

    opportunities: list[Opportunity] = []

    async def get_side_24h(exchange: str, market: str) -> float:
        value = await fetch_24h_volume(session, exchange, symbol, market)
        return value if value is not None else 0.0

    if settings.mode in ("all", "spread", "futures_futures", "funding") and len(futures_ex) >= 2:
        books = {ex: book for ex, book in zip(futures_ex, await asyncio.gather(*[fetch_futures_book(session, ex, symbol) for ex in futures_ex])) if book}
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
                            f_long,
                            f_short,
                        )
                    )

    if settings.mode in ("all", "spread", "spot_futures") and spot_ex and futures_ex:
        spot_books = {ex: book for ex, book in zip(spot_ex, await asyncio.gather(*[fetch_spot_book(session, ex, symbol) for ex in spot_ex])) if book}
        fut_books = {ex: book for ex, book in zip(futures_ex, await asyncio.gather(*[fetch_futures_book(session, ex, symbol) for ex in futures_ex])) if book}
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
                )
            )

    for opp in opportunities:
        if opp.max_executable_size_usdt < settings.min_volume_usdt:
            continue
        if opp.min_24h_volume_usdt < settings.min_24h_volume_usdt:
            continue
        if opp.mode == "funding":
            if opp.total_edge_pct < settings.min_profit_pct:
                continue
        else:
            if opp.net_spread_pct < settings.min_profit_pct:
                continue

        if on_cooldown(opp.symbol, opp.mode, opp.buy_exchange, opp.sell_exchange):
            continue

        await tg.send_message(cfg.TELEGRAM_CHAT_ID, format_signal(opp))


async def start_telegram_bot() -> Application:
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set in .env")

    allowed_chat_id = str(cfg.TELEGRAM_CHAT_ID)

    async def auth_check(update: Update) -> bool:
        chat_id = str(update.effective_chat.id) if update.effective_chat else ""
        if chat_id != allowed_chat_id:
            if update.message:
                await update.message.reply_text("🔒 Access denied")
            return False
        return True

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        await update.message.reply_text("Bot online. Commands: /show /set /mode /reset", parse_mode="HTML")

    async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        symbols = "ALL" if not settings.filtered_symbols else ",".join(sorted(settings.filtered_symbols))
        await update.message.reply_text(
            f"mode={settings.mode}\n"
            f"min_profit={settings.min_profit_pct}\n"
            f"min_volume={settings.min_volume_usdt}\n"
            f"min_funding={settings.min_funding_pct} (info only)\n"
            f"min_24h_volume={settings.min_24h_volume_usdt}\n"
            f"symbols={symbols}\n"
            f"log_max_mb={settings.log_max_mb}\n"
            f"log_backups={settings.log_backups}"
        )

    async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        settings.reset_to_default()
        settings.save_to_file(SETTINGS_FILE)
        await update.message.reply_text("Settings reset")

    async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("Usage: /set min_profit 0.2 | /set min_volume 1000 | /set min_24h_volume 500000 | /set min_funding 0.03 | /set symbols BTC,ETH | /set symbols ALL")
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
            elif key == "symbols":
                if value.upper() == "ALL":
                    settings.filtered_symbols = None
                else:
                    settings.filtered_symbols = {s.strip().upper() for s in value.split(",") if s.strip()}
            else:
                await update.message.reply_text("Unknown key")
                return
            settings.save_to_file(SETTINGS_FILE)
            await update.message.reply_text("Updated")
        except ValueError:
            await update.message.reply_text("Invalid value")

    async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth_check(update):
            return
        if not context.args:
            await update.message.reply_text("Usage: /mode spread|funding|spot_futures|futures_futures|all")
            return
        mode = context.args[0].lower()
        allowed = {"spread", "funding", "spot_futures", "futures_futures", "all"}
        if mode not in allowed:
            await update.message.reply_text("Invalid mode")
            return
        settings.mode = mode
        settings.save_to_file(SETTINGS_FILE)
        await update.message.reply_text(f"Mode set to {mode}")

    app_builder = Application.builder().token(cfg.TELEGRAM_TOKEN)
    if cfg.PROXY_LIST:
        app_builder = app_builder.proxy(cfg.PROXY_LIST[0]).get_updates_proxy(cfg.PROXY_LIST[0])
    app = app_builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("reset", cmd_reset))

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
    async with aiohttp.ClientSession(connector=connector) as session:
        symbol_maps = await fetch_symbol_maps(session)
        last_update = time.time()

        while True:
            if time.time() - last_update > cfg.UPDATE_PAIRS_INTERVAL:
                symbol_maps = await fetch_symbol_maps(session)
                last_update = time.time()

            all_symbols = sorted(set().union(*symbol_maps["futures"].values(), *symbol_maps["spot"].values()))
            scan_symbols = [s for s in all_symbols if not settings.filtered_symbols or s in settings.filtered_symbols]

            tasks = [scan_symbol(session, tg, symbol, symbol_maps) for symbol in scan_symbols]
            for i in range(0, len(tasks), cfg.BATCH_SIZE):
                batch = tasks[i : i + cfg.BATCH_SIZE]
                await asyncio.gather(*batch, return_exceptions=True)
                await asyncio.sleep(0.3)

            await asyncio.sleep(cfg.SCAN_INTERVAL)

    await app.updater.stop()
    await app.stop()
    await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
