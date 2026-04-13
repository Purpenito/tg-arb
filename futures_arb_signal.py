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
from typing import Any, Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import config as cfg
from proxy_manager import TelegramProxyManager

SETTINGS_FILE = "settings.json"
LOG_FILE = Path("logs/arb_bot.log")

logger = logging.getLogger("arb_bot")


@dataclass
class BookSnapshot:
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]


@dataclass
class Opportunity:
    symbol: str
    mode: str
    long_exchange: str
    short_exchange: str
    top_long_price: float
    top_short_price: float
    avg_long_fill_price: float
    avg_short_fill_price: float
    top_level_size_usdt: float
    max_executable_size_usdt: float
    capital_usdt: float
    trade_size_usdt: float
    gross_spread_pct: float
    total_fees_pct: float
    net_spread_pct: float
    funding_long: Optional[float]
    funding_short: Optional[float]
    net_funding_pct: float
    total_edge_pct: float
    volume_24h_usdt: float
    estimated_spread_pnl_usdt: float
    estimated_funding_pnl_usdt: float
    estimated_total_pnl_usdt: float
    long_link: str
    short_link: str
    updated_at: str


class Settings:
    def __init__(self) -> None:
        self.capital_usdt = cfg.DEFAULT_CAPITAL_USDT
        self.min_profit_pct = cfg.DEFAULT_MIN_PROFIT_PCT
        self.min_trade_size_usdt = cfg.DEFAULT_MIN_TRADE_SIZE_USDT
        self.min_24h_volume_usdt = cfg.DEFAULT_MIN_24H_VOLUME_USDT
        self.min_funding_pct = cfg.DEFAULT_MIN_FUNDING_PCT
        self.max_negative_entry_spread_pct = cfg.DEFAULT_MAX_NEGATIVE_ENTRY_SPREAD_PCT
        self.filtered_symbols: Optional[set[str]] = set(cfg.DEFAULT_FILTERED_SYMBOLS) if cfg.DEFAULT_FILTERED_SYMBOLS else None
        self.enabled_exchanges = list(cfg.DEFAULT_ENABLED_EXCHANGES)
        self.mode = cfg.DEFAULT_MODE
        self.auto_alerts_enabled = cfg.DEFAULT_AUTO_ALERTS_ENABLED
        self.cooldown_minutes = cfg.DEFAULT_COOLDOWN_MINUTES
        self.max_results = cfg.DEFAULT_MAX_RESULTS
        self.only_positive_signals = cfg.DEFAULT_ONLY_POSITIVE_SIGNALS
        self.funding_weight = cfg.DEFAULT_FUNDING_WEIGHT
        self.log_max_mb = cfg.DEFAULT_LOG_MAX_MB
        self.log_backups = cfg.DEFAULT_LOG_BACKUPS

    def to_dict(self) -> dict[str, Any]:
        return {
            "capital_usdt": self.capital_usdt,
            "min_profit_pct": self.min_profit_pct,
            "min_trade_size_usdt": self.min_trade_size_usdt,
            "min_24h_volume_usdt": self.min_24h_volume_usdt,
            "min_funding_pct": self.min_funding_pct,
            "max_negative_entry_spread_pct": self.max_negative_entry_spread_pct,
            "filtered_symbols": sorted(self.filtered_symbols) if self.filtered_symbols else None,
            "enabled_exchanges": self.enabled_exchanges,
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
        self.capital_usdt = float(data.get("capital_usdt", cfg.DEFAULT_CAPITAL_USDT))
        self.min_profit_pct = float(data.get("min_profit_pct", cfg.DEFAULT_MIN_PROFIT_PCT))
        self.min_trade_size_usdt = float(data.get("min_trade_size_usdt", cfg.DEFAULT_MIN_TRADE_SIZE_USDT))
        self.min_24h_volume_usdt = float(data.get("min_24h_volume_usdt", cfg.DEFAULT_MIN_24H_VOLUME_USDT))
        self.min_funding_pct = float(data.get("min_funding_pct", cfg.DEFAULT_MIN_FUNDING_PCT))
        self.max_negative_entry_spread_pct = float(data.get("max_negative_entry_spread_pct", cfg.DEFAULT_MAX_NEGATIVE_ENTRY_SPREAD_PCT))
        syms = data.get("filtered_symbols")
        self.filtered_symbols = {str(s).upper() for s in syms} if syms else None
        enabled = [str(e).lower() for e in data.get("enabled_exchanges", cfg.DEFAULT_ENABLED_EXCHANGES)]
        self.enabled_exchanges = [e for e in enabled if e in cfg.EXCHANGES] or list(cfg.DEFAULT_ENABLED_EXCHANGES)
        self.mode = str(data.get("mode", cfg.DEFAULT_MODE)).lower()
        self.auto_alerts_enabled = bool(data.get("auto_alerts_enabled", cfg.DEFAULT_AUTO_ALERTS_ENABLED))
        self.cooldown_minutes = max(1, int(data.get("cooldown_minutes", cfg.DEFAULT_COOLDOWN_MINUTES)))
        self.max_results = max(1, int(data.get("max_results", cfg.DEFAULT_MAX_RESULTS)))
        self.only_positive_signals = bool(data.get("only_positive_signals", cfg.DEFAULT_ONLY_POSITIVE_SIGNALS))
        self.funding_weight = float(data.get("funding_weight", cfg.DEFAULT_FUNDING_WEIGHT))
        self.log_max_mb = max(1, int(data.get("log_max_mb", cfg.DEFAULT_LOG_MAX_MB)))
        self.log_backups = max(1, int(data.get("log_backups", cfg.DEFAULT_LOG_BACKUPS)))

    def load(self) -> None:
        if not os.path.exists(SETTINGS_FILE):
            self.save()
            return
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                self.from_dict(json.load(fh))
        except Exception as exc:
            logger.warning("settings load error: %s", exc)
            self.save()

    def save(self) -> None:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, ensure_ascii=False)
        os.replace(tmp, SETTINGS_FILE)


class Diagnostics:
    def __init__(self) -> None:
        self.symbols_checked = 0
        self.signals_sent = 0
        self.participation_count: Counter[str] = Counter()
        self.long_count: Counter[str] = Counter()
        self.short_count: Counter[str] = Counter()
        self.cut_reasons: dict[str, Counter[str]] = defaultdict(Counter)
        self.funding_candidates = 0

    def cut(self, exchange: str, reason: str) -> None:
        self.cut_reasons[exchange][reason] += 1

    def reset(self) -> None:
        self.__init__()

    def log_summary(self, cycle_no: int) -> None:
        logger.info(
            "SUMMARY cycle=%s symbols_checked=%s signals_sent=%s funding_candidates=%s participation=%s long=%s short=%s cuts=%s",
            cycle_no,
            self.symbols_checked,
            self.signals_sent,
            self.funding_candidates,
            dict(self.participation_count),
            dict(self.long_count),
            dict(self.short_count),
            {k: dict(v) for k, v in self.cut_reasons.items()},
        )


settings = Settings()
diagnostics = Diagnostics()
last_sent: dict[str, float] = {}
latest_candidates: dict[str, Opportunity] = {}
latest_debug: dict[str, dict[str, Any]] = {}


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > cfg.PRESTART_MAX_LOG_MB * 1024 * 1024:
        LOG_FILE.rename(LOG_FILE.with_suffix(".log.prestart"))

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(LOG_FILE, maxBytes=settings.log_max_mb * 1024 * 1024, backupCount=settings.log_backups, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


def cooldown_ok(key: str) -> bool:
    now = time.time()
    cd = settings.cooldown_minutes * 60
    if now - last_sent.get(key, 0) < cd:
        return False
    last_sent[key] = now
    return True


def funding_text(rate: Optional[float], side: str) -> str:
    if rate is None:
        return "N/A"
    pct = abs(rate) * 100
    if side == "LONG":
        return f"LONG {'pays' if rate > 0 else 'receives'} {pct:.4f}%" if rate != 0 else "LONG funding 0.0000%"
    return f"SHORT {'receives' if rate > 0 else 'pays'} {pct:.4f}%" if rate != 0 else "SHORT funding 0.0000%"


def deep_link(exchange: str, symbol: str) -> str:
    m = {
        "bybit": f"https://www.bybit.com/trade/usdt/{symbol}USDT",
        "kucoin": f"https://www.kucoin.com/futures/trade/{symbol}USDTM",
        "okx": f"https://www.okx.com/trade-swap/{symbol.lower()}-usdt-swap",
        "bingx": f"https://bingx.com/en-us/futures/forward/{symbol}USDT",
        "bitget": f"https://www.bitget.com/futures/usdt/{symbol}USDT",
        "gate": f"https://www.gate.io/futures_trade/USDT/{symbol}_USDT",
    }
    return m.get(exchange, "")


def pair_symbol(exchange: str, symbol: str) -> str:
    return {
        "bybit": f"{symbol}USDT",
        "kucoin": f"{symbol}USDTM",
        "okx": f"{symbol}-USDT-SWAP",
        "bingx": f"{symbol}-USDT",
        "bitget": f"{symbol}USDT",
        "gate": f"{symbol}_USDT",
    }[exchange]


async def fetch_json(session: aiohttp.ClientSession, url: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
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


async def fetch_symbols(session: aiohttp.ClientSession, exchange: str) -> set[str]:
    out: set[str] = set()
    try:
        if exchange == "bybit":
            d, _ = await fetch_json(session, "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000")
            for i in (d or {}).get("result", {}).get("list", []):
                if i.get("quoteCoin") == "USDT" and i.get("status") == "Trading":
                    out.add(i.get("baseCoin"))
        elif exchange == "kucoin":
            d, _ = await fetch_json(session, "https://api-futures.kucoin.com/api/v1/contracts/active")
            for i in (d or {}).get("data", []):
                if i.get("settleCurrency") == "USDT" and i.get("status") == "Open":
                    out.add(i.get("baseCurrency"))
        elif exchange == "okx":
            d, _ = await fetch_json(session, "https://www.okx.com/api/v5/public/instruments?instType=SWAP")
            for i in (d or {}).get("data", []):
                inst = i.get("instId", "")
                if inst.endswith("-USDT-SWAP") and i.get("state") == "live":
                    out.add(inst.split("-")[0])
        elif exchange == "bingx":
            d, _ = await fetch_json(session, "https://open-api.bingx.com/openApi/swap/v2/quote/contracts")
            for i in (d or {}).get("data", []):
                s = i.get("symbol", "")
                if s.endswith("-USDT") and i.get("status") == 1:
                    out.add(s.replace("-USDT", ""))
        elif exchange == "bitget":
            d, _ = await fetch_json(session, "https://api.bitget.com/api/v2/mix/market/contracts?productType=USDT-FUTURES")
            for i in (d or {}).get("data", []):
                sym = i.get("symbol", "")
                if sym.endswith("USDT"):
                    out.add(sym.replace("USDT", ""))
        elif exchange == "gate":
            d, _ = await fetch_json(session, "https://api.gateio.ws/api/v4/futures/usdt/contracts")
            for i in d or []:
                name = i.get("name", "")
                if name.endswith("_USDT") and i.get("in_delisting") is False:
                    out.add(name.replace("_USDT", ""))
    except Exception:
        return set()
    return {x for x in out if x}


async def fetch_depth(session: aiohttp.ClientSession, exchange: str, symbol: str) -> tuple[Optional[BookSnapshot], Optional[str]]:
    p = pair_symbol(exchange, symbol)
    if exchange == "bybit":
        d, e = await fetch_json(session, f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={p}&limit={cfg.DEPTH_LEVELS}")
        if e:
            return None, e
        b = [(float(x[0]), float(x[1])) for x in (d or {}).get("result", {}).get("b", [])]
        a = [(float(x[0]), float(x[1])) for x in (d or {}).get("result", {}).get("a", [])]
        return (BookSnapshot(bids=b, asks=a), None) if b and a else (None, "no orderbook")
    if exchange == "kucoin":
        d, e = await fetch_json(session, f"https://api-futures.kucoin.com/api/v1/level2/snapshot?symbol={p}")
        if e:
            return None, e
        dd = (d or {}).get("data", {})
        b = [(float(x[0]), float(x[1])) for x in dd.get("bids", [])[: cfg.DEPTH_LEVELS]]
        a = [(float(x[0]), float(x[1])) for x in dd.get("asks", [])[: cfg.DEPTH_LEVELS]]
        return (BookSnapshot(bids=b, asks=a), None) if b and a else (None, "no orderbook")
    if exchange == "okx":
        d, e = await fetch_json(session, f"https://www.okx.com/api/v5/market/books?instId={p}&sz={cfg.DEPTH_LEVELS}")
        if e:
            return None, e
        row = ((d or {}).get("data") or [{}])[0]
        b = [(float(x[0]), float(x[1])) for x in row.get("bids", [])]
        a = [(float(x[0]), float(x[1])) for x in row.get("asks", [])]
        return (BookSnapshot(bids=b, asks=a), None) if b and a else (None, "no orderbook")
    if exchange == "bingx":
        d, e = await fetch_json(session, f"https://open-api.bingx.com/openApi/swap/v2/quote/depth?symbol={p}&limit={cfg.DEPTH_LEVELS}")
        if e:
            return None, e
        row = (d or {}).get("data", {})
        b = [(float(x[0]), float(x[1])) for x in row.get("bids", [])]
        a = [(float(x[0]), float(x[1])) for x in row.get("asks", [])]
        return (BookSnapshot(bids=b, asks=a), None) if b and a else (None, "no orderbook")
    if exchange == "bitget":
        d, e = await fetch_json(session, f"https://api.bitget.com/api/v2/mix/market/merge-depth?symbol={p}&productType=USDT-FUTURES&precision=scale0&limit={cfg.DEPTH_LEVELS}")
        if e:
            return None, e
        row = (d or {}).get("data", {})
        b = [(float(x[0]), float(x[1])) for x in row.get("bids", [])]
        a = [(float(x[0]), float(x[1])) for x in row.get("asks", [])]
        return (BookSnapshot(bids=b, asks=a), None) if b and a else (None, "no orderbook")
    if exchange == "gate":
        d, e = await fetch_json(session, f"https://api.gateio.ws/api/v4/futures/usdt/order_book?contract={p}&limit={cfg.DEPTH_LEVELS}")
        if e:
            return None, e
        b = [(float(x["p"]), float(x["s"])) for x in (d or {}).get("bids", [])]
        a = [(float(x["p"]), float(x["s"])) for x in (d or {}).get("asks", [])]
        return (BookSnapshot(bids=b, asks=a), None) if b and a else (None, "no orderbook")
    return None, "no orderbook"


async def fetch_funding(session: aiohttp.ClientSession, exchange: str, symbol: str) -> Optional[float]:
    p = pair_symbol(exchange, symbol)
    try:
        if exchange == "bybit":
            d, _ = await fetch_json(session, f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={p}")
            rows = (d or {}).get("result", {}).get("list", [])
            return float(rows[0].get("fundingRate", 0)) if rows else None
        if exchange == "kucoin":
            d, _ = await fetch_json(session, f"https://api-futures.kucoin.com/api/v1/contracts/{p}")
            return float((d or {}).get("data", {}).get("fundingFeeRate", 0))
        if exchange == "okx":
            d, _ = await fetch_json(session, f"https://www.okx.com/api/v5/public/funding-rate?instId={p}")
            rows = (d or {}).get("data", [])
            return float(rows[0].get("fundingRate", 0)) if rows else None
        if exchange == "bingx":
            d, _ = await fetch_json(session, f"https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex?symbol={p}")
            return float((d or {}).get("data", {}).get("lastFundingRate", 0))
        if exchange == "bitget":
            d, _ = await fetch_json(session, f"https://api.bitget.com/api/v2/mix/market/current-fund-rate?symbol={p}&productType=USDT-FUTURES")
            rows = (d or {}).get("data", [])
            return float(rows[0].get("fundingRate", 0)) if rows else None
        if exchange == "gate":
            d, _ = await fetch_json(session, f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{p}")
            return float((d or {}).get("funding_rate", 0))
    except Exception:
        return None
    return None


async def fetch_24h_volume(session: aiohttp.ClientSession, exchange: str, symbol: str) -> float:
    p = pair_symbol(exchange, symbol)
    try:
        if exchange == "bybit":
            d, _ = await fetch_json(session, f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={p}")
            rows = (d or {}).get("result", {}).get("list", [])
            return float(rows[0].get("turnover24h", 0)) if rows else 0.0
        if exchange == "kucoin":
            d, _ = await fetch_json(session, f"https://api-futures.kucoin.com/api/v1/contracts/{p}")
            return float((d or {}).get("data", {}).get("turnoverOf24h", 0))
        if exchange == "okx":
            d, _ = await fetch_json(session, f"https://www.okx.com/api/v5/market/ticker?instId={p}")
            rows = (d or {}).get("data", [])
            return float(rows[0].get("volCcy24h", 0)) if rows else 0.0
        if exchange == "bingx":
            d, _ = await fetch_json(session, f"https://open-api.bingx.com/openApi/swap/v2/quote/ticker?symbol={p}")
            return float((d or {}).get("data", {}).get("quoteVolume", 0) or 0)
        if exchange == "bitget":
            d, _ = await fetch_json(session, f"https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES")
            for row in (d or {}).get("data", []):
                if row.get("symbol") == p:
                    return float(row.get("usdtVolume", 0) or 0)
        if exchange == "gate":
            d, _ = await fetch_json(session, "https://api.gateio.ws/api/v4/futures/usdt/tickers")
            for row in d or []:
                if row.get("contract") == p:
                    return float(row.get("volume_24h_quote", 0) or 0)
    except Exception:
        return 0.0
    return 0.0


def calc_fee_pct(long_ex: str, short_ex: str) -> float:
    return (cfg.FEES.get(long_ex, {}).get("futures", 0.001) + cfg.FEES.get(short_ex, {}).get("futures", 0.001)) * 100


def execute_size_and_avg(levels: list[tuple[float, float]], size_usdt: float, is_buy: bool) -> tuple[float, float]:
    remaining = size_usdt
    total_qty = 0.0
    weighted = 0.0
    for price, qty in levels:
        notional = price * qty
        use = min(remaining, notional)
        if use <= 0:
            break
        q = use / price
        total_qty += q
        weighted += q * price
        remaining -= use
    if total_qty == 0:
        return 0.0, 0.0
    return total_qty, weighted / total_qty


def max_notional(levels: list[tuple[float, float]]) -> float:
    return sum(p * q for p, q in levels)


def format_signal(o: Opportunity) -> str:
    return (
        f"💎 <b>{o.symbol}</b> | <code>{o.mode}</code>\n"
        f"LONG: <b>{o.long_exchange.upper()}</b> ({o.long_link})\n"
        f"SHORT: <b>{o.short_exchange.upper()}</b> ({o.short_link})\n"
        f"Prices top long/short: <code>{o.top_long_price:.6f}</code> / <code>{o.top_short_price:.6f}</code>\n"
        f"Avg fill long/short: <code>{o.avg_long_fill_price:.6f}</code> / <code>{o.avg_short_fill_price:.6f}</code>\n"
        f"Top level size: <code>{o.top_level_size_usdt:,.2f} USDT</code>\n"
        f"Max executable: <code>{o.max_executable_size_usdt:,.2f} USDT</code>\n"
        f"Capital/Trade size: <code>{o.capital_usdt:,.2f}</code> / <code>{o.trade_size_usdt:,.2f}</code> USDT\n"
        f"Gross spread: <code>{o.gross_spread_pct:+.4f}%</code>\n"
        f"Fees: <code>{o.total_fees_pct:.4f}%</code>\n"
        f"Net spread: <code>{o.net_spread_pct:+.4f}%</code>\n"
        f"Funding long/short: <code>{'N/A' if o.funding_long is None else f'{o.funding_long*100:.4f}%'} / {'N/A' if o.funding_short is None else f'{o.funding_short*100:.4f}%'}</code>\n"
        f"Funding explain: <code>{funding_text(o.funding_long, 'LONG')} | {funding_text(o.funding_short, 'SHORT')}</code>\n"
        f"Funding edge: <code>{o.net_funding_pct:+.4f}%</code>\n"
        f"Total edge: <b>{o.total_edge_pct:+.4f}%</b>\n"
        f"24h volume (min): <code>{o.volume_24h_usdt:,.2f} USDT</code>\n"
        f"PnL spread/funding/total: <code>{o.estimated_spread_pnl_usdt:,.2f}</code> / <code>{o.estimated_funding_pnl_usdt:,.2f}</code> / <b>{o.estimated_total_pnl_usdt:,.2f} USDT</b>\n"
        f"Updated: {o.updated_at}"
    )


async def evaluate_symbol(session: aiohttp.ClientSession, symbol: str, mode: str) -> list[Opportunity]:
    diagnostics.symbols_checked += 1
    results: dict[str, BookSnapshot] = {}
    reasons: list[str] = []

    for ex in settings.enabled_exchanges:
        depth, reason = await fetch_depth(session, ex, symbol)
        if depth:
            results[ex] = depth
            diagnostics.participation_count[ex] += 1
        else:
            diagnostics.cut(ex, reason or "no orderbook")
            reasons.append(f"{ex}:{reason}")

    latest_debug[symbol] = {"symbol": symbol, "orderbook_exchanges": sorted(results.keys()), "cuts": reasons, "best": None}

    if len(results) < 2:
        return []

    long_ex = min(results.keys(), key=lambda ex: results[ex].asks[0][0])
    short_ex = max(results.keys(), key=lambda ex: results[ex].bids[0][0])
    if long_ex == short_ex:
        diagnostics.cut(long_ex, "same exchange")
        return []

    long_book = results[long_ex]
    short_book = results[short_ex]
    top_level_size = min(long_book.asks[0][0] * long_book.asks[0][1], short_book.bids[0][0] * short_book.bids[0][1])

    max_exec = min(max_notional(long_book.asks), max_notional(short_book.bids))
    trade_size = min(settings.capital_usdt, max_exec)
    if trade_size <= 0:
        return []

    _, avg_long = execute_size_and_avg(long_book.asks, trade_size, True)
    _, avg_short = execute_size_and_avg(short_book.bids, trade_size, False)
    if avg_long <= 0 or avg_short <= 0:
        diagnostics.cut(long_ex, "no executable avg fill")
        diagnostics.cut(short_ex, "no executable avg fill")
        return []

    gross = ((avg_short - avg_long) / avg_long) * 100
    fees_pct = calc_fee_pct(long_ex, short_ex)
    net_spread = gross - fees_pct

    vol_long, vol_short, f_long, f_short = await asyncio.gather(
        fetch_24h_volume(session, long_ex, symbol),
        fetch_24h_volume(session, short_ex, symbol),
        fetch_funding(session, long_ex, symbol),
        fetch_funding(session, short_ex, symbol),
    )
    min_vol = min(vol_long, vol_short)

    funding_edge = 0.0
    if f_long is not None and f_short is not None:
        funding_edge = (f_short - f_long) * 100
    total_edge = net_spread + funding_edge * settings.funding_weight

    opp_mode = "funding" if mode == "funding" else "futures_futures"
    opp = Opportunity(
        symbol=symbol,
        mode=opp_mode,
        long_exchange=long_ex,
        short_exchange=short_ex,
        top_long_price=long_book.asks[0][0],
        top_short_price=short_book.bids[0][0],
        avg_long_fill_price=avg_long,
        avg_short_fill_price=avg_short,
        top_level_size_usdt=top_level_size,
        max_executable_size_usdt=max_exec,
        capital_usdt=settings.capital_usdt,
        trade_size_usdt=trade_size,
        gross_spread_pct=gross,
        total_fees_pct=fees_pct,
        net_spread_pct=net_spread,
        funding_long=f_long,
        funding_short=f_short,
        net_funding_pct=funding_edge,
        total_edge_pct=total_edge,
        volume_24h_usdt=min_vol,
        estimated_spread_pnl_usdt=trade_size * net_spread / 100,
        estimated_funding_pnl_usdt=trade_size * funding_edge / 100,
        estimated_total_pnl_usdt=trade_size * total_edge / 100,
        long_link=deep_link(long_ex, symbol),
        short_link=deep_link(short_ex, symbol),
        updated_at=now_utc(),
    )

    latest_debug[symbol]["best"] = {
        "long": long_ex,
        "short": short_ex,
        "net_spread_pct": net_spread,
        "net_funding_pct": funding_edge,
        "total_edge_pct": total_edge,
    }

    filtered: list[Opportunity] = []
    if opp.trade_size_usdt < settings.min_trade_size_usdt:
        diagnostics.cut(long_ex, f"low trade size got={opp.trade_size_usdt:.2f} required={settings.min_trade_size_usdt:.2f}")
        diagnostics.cut(short_ex, f"low trade size got={opp.trade_size_usdt:.2f} required={settings.min_trade_size_usdt:.2f}")
        return []
    if opp.volume_24h_usdt < settings.min_24h_volume_usdt:
        diagnostics.cut(long_ex, f"low 24h volume got={opp.volume_24h_usdt:.2f} required={settings.min_24h_volume_usdt:.2f}")
        diagnostics.cut(short_ex, f"low 24h volume got={opp.volume_24h_usdt:.2f} required={settings.min_24h_volume_usdt:.2f}")
        return []
    if mode == "spread":
        if opp.net_spread_pct < settings.min_profit_pct:
            diagnostics.cut(long_ex, f"bad spread edge got={opp.net_spread_pct:.4f} required={settings.min_profit_pct:.4f}")
            diagnostics.cut(short_ex, f"bad spread edge got={opp.net_spread_pct:.4f} required={settings.min_profit_pct:.4f}")
            return []
        filtered.append(opp)
    elif mode == "funding":
        diagnostics.funding_candidates += 1
        if opp.net_spread_pct < -settings.max_negative_entry_spread_pct:
            diagnostics.cut(long_ex, "bad funding edge")
            diagnostics.cut(short_ex, "bad funding edge")
            return []
        if opp.net_funding_pct < settings.min_funding_pct:
            diagnostics.cut(long_ex, f"bad funding edge got={opp.net_funding_pct:.4f} required={settings.min_funding_pct:.4f}")
            diagnostics.cut(short_ex, f"bad funding edge got={opp.net_funding_pct:.4f} required={settings.min_funding_pct:.4f}")
            return []
        if opp.total_edge_pct < settings.min_profit_pct:
            diagnostics.cut(long_ex, f"bad total edge got={opp.total_edge_pct:.4f} required={settings.min_profit_pct:.4f}")
            diagnostics.cut(short_ex, f"bad total edge got={opp.total_edge_pct:.4f} required={settings.min_profit_pct:.4f}")
            return []
        filtered.append(opp)
    else:
        if opp.net_spread_pct >= settings.min_profit_pct:
            filtered.append(opp)
        elif opp.net_funding_pct >= settings.min_funding_pct and opp.total_edge_pct >= settings.min_profit_pct and opp.net_spread_pct >= -settings.max_negative_entry_spread_pct:
            opp.mode = "funding"
            filtered.append(opp)
        else:
            diagnostics.cut(long_ex, "bad total edge")
            diagnostics.cut(short_ex, "bad total edge")

    if settings.only_positive_signals:
        filtered = [x for x in filtered if x.estimated_total_pnl_usdt > 0]

    return filtered


async def scan_market(session: aiohttp.ClientSession, mode: str) -> list[Opportunity]:
    symbol_map = await asyncio.gather(*[fetch_symbols(session, ex) for ex in settings.enabled_exchanges])
    merged = sorted(set().union(*symbol_map))
    if settings.filtered_symbols:
        merged = [s for s in merged if s in settings.filtered_symbols]

    all_opps: list[Opportunity] = []
    for i in range(0, len(merged), cfg.BATCH_SIZE):
        batch = merged[i : i + cfg.BATCH_SIZE]
        results = await asyncio.gather(*[evaluate_symbol(session, s, mode) for s in batch], return_exceptions=True)
        for val in results:
            if isinstance(val, list):
                all_opps.extend(val)

    all_opps.sort(key=lambda x: x.total_edge_pct, reverse=True)
    return all_opps


def status_text() -> str:
    syms = "ALL" if not settings.filtered_symbols else ",".join(sorted(settings.filtered_symbols))
    return (
        f"Режим: {settings.mode}\n"
        f"Автоуведомления: {'ВКЛ' if settings.auto_alerts_enabled else 'ВЫКЛ'}\n"
        f"Капитал: ${settings.capital_usdt:,.0f} USDT\n"
        f"Активные биржи: {', '.join(settings.enabled_exchanges)}\n"
        f"Монеты: {syms}\n"
        f"Мин. профит: {settings.min_profit_pct:.3f}%\n"
        f"Мин. 24ч объём: ${settings.min_24h_volume_usdt:,.0f} USDT"
    )


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Проверить сигналы", callback_data="scan:all")],
        [InlineKeyboardButton("Futures → Futures", callback_data="scan:spread"), InlineKeyboardButton("Futures + Funding", callback_data="scan:funding")],
        [InlineKeyboardButton("Уведомления", callback_data="alerts:toggle"), InlineKeyboardButton("Статус", callback_data="ui:status")],
        [InlineKeyboardButton("Помощь", callback_data="ui:help")],
    ])


async def start_telegram_bot() -> Application:
    if not cfg.TELEGRAM_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID are required")

    allowed_chat = str(cfg.TELEGRAM_CHAT_ID)

    async def auth(update: Update) -> bool:
        msg = update.effective_message
        if str(update.effective_chat.id) != allowed_chat:
            if msg:
                await msg.reply_text("🔒 Access denied")
            return False
        return True

    async def send_top(msg, mode: str) -> None:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=50)) as session:
            opps = await scan_market(session, mode)
        if not opps:
            await msg.reply_text("Сигналов не найдено по текущим фильтрам.")
            return
        preview = "\n".join([f"{i+1}. {o.symbol} — LONG {o.long_exchange.upper()} / SHORT {o.short_exchange.upper()} — {o.total_edge_pct:+.3f}%" for i, o in enumerate(opps[: settings.max_results])])
        await msg.reply_text(f"Топ сигналов сейчас:\n{preview}")
        for o in opps[: settings.max_results]:
            latest_candidates[o.symbol] = o

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth(update):
            return
        msg = update.effective_message
        if not msg:
            return
        await msg.reply_text(f"🚀 Арбитраж-терминал\n\n{status_text()}", reply_markup=main_menu())

    async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth(update):
            return
        msg = update.effective_message
        if msg:
            await msg.reply_text(status_text())

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await cmd_show(update, context)

    async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth(update):
            return
        settings.__init__()
        settings.save()
        msg = update.effective_message
        if msg:
            await msg.reply_text("Настройки сброшены.", reply_markup=main_menu())

    async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth(update):
            return
        msg = update.effective_message
        if not msg:
            return
        if len(context.args) < 2:
            await msg.reply_text("Usage: /set capital 1000 | /set min_profit 0.15 | /set min_trade_size 500 | /set min_24h_volume 1000000 | /set min_funding 0.01 | /set symbols BTC,ETH | /set symbols ALL")
            return
        key = context.args[0].lower()
        value = " ".join(context.args[1:]).strip()
        try:
            if key == "capital":
                settings.capital_usdt = float(value)
            elif key == "min_profit":
                settings.min_profit_pct = float(value)
            elif key == "min_trade_size":
                settings.min_trade_size_usdt = float(value)
            elif key == "min_24h_volume":
                settings.min_24h_volume_usdt = float(value)
            elif key == "min_funding":
                settings.min_funding_pct = float(value)
            elif key == "cooldown":
                settings.cooldown_minutes = int(float(value))
            elif key == "max_results":
                settings.max_results = int(float(value))
            elif key == "funding_weight":
                settings.funding_weight = float(value)
            elif key == "alerts":
                settings.auto_alerts_enabled = value.lower() in {"on", "1", "true", "yes", "вкл"}
            elif key == "symbols":
                settings.filtered_symbols = None if value.upper() == "ALL" else {x.strip().upper() for x in value.split(",") if x.strip()}
            elif key == "exchanges":
                selected = [x.strip().lower() for x in value.split(",") if x.strip().lower() in cfg.EXCHANGES]
                if selected:
                    settings.enabled_exchanges = selected
            else:
                await msg.reply_text("Unknown key")
                return
            settings.save()
            await msg.reply_text("✅ Updated")
        except ValueError:
            await msg.reply_text("Invalid value")

    async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth(update):
            return
        msg = update.effective_message
        if not msg:
            return
        if not context.args:
            await msg.reply_text("/mode spread|funding|all")
            return
        mode = context.args[0].lower()
        if mode not in {"spread", "funding", "all"}:
            await msg.reply_text("invalid mode")
            return
        settings.mode = mode
        settings.save()
        await msg.reply_text(f"Mode = {mode}")

    async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth(update):
            return
        msg = update.effective_message
        if not msg:
            return
        if not context.args:
            await msg.reply_text("Usage: /debug ETH")
            return
        sym = context.args[0].upper()
        d = latest_debug.get(sym)
        if not d:
            await msg.reply_text("Нет debug данных по монете.")
            return
        await msg.reply_text(f"DEBUG {sym}\norderbook_exchanges={d.get('orderbook_exchanges')}\ncuts={d.get('cuts')}\nbest={d.get('best')}")

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await auth(update):
            return
        q = update.callback_query
        if not q:
            return
        await q.answer()
        if q.data == "ui:status":
            await q.message.reply_text(status_text())
            return
        if q.data == "ui:help":
            await q.message.reply_text(
                "Futures-Futures: спред между двумя фьючерсами.\n"
                "Funding edge: разница funding short-long.\n"
                "Total edge = net spread + funding edge * funding_weight.\n"
                "Executable size: реальный размер по стакану."
            )
            return
        if q.data == "alerts:toggle":
            settings.auto_alerts_enabled = not settings.auto_alerts_enabled
            settings.save()
            await q.message.reply_text(f"Автоуведомления: {'ВКЛ' if settings.auto_alerts_enabled else 'ВЫКЛ'}")
            return
        if q.data.startswith("scan:"):
            mode = q.data.split(":", 1)[1]
            await send_top(q.message, mode)

    app_builder = Application.builder().token(cfg.TELEGRAM_TOKEN)
    if cfg.PROXY_LIST:
        app_builder = app_builder.proxy(cfg.PROXY_LIST[0]).get_updates_proxy(cfg.PROXY_LIST[0])
    app = app_builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CallbackQueryHandler(on_button))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    return app


async def alerts_loop(tg: TelegramProxyManager) -> None:
    connector = aiohttp.TCPConnector(limit=80)
    cycle = 0
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            cycle += 1
            try:
                mode = settings.mode if settings.mode in {"spread", "funding"} else "all"
                opps = await scan_market(session, mode)
                for o in opps[: settings.max_results]:
                    latest_candidates[o.symbol] = o
                    if settings.auto_alerts_enabled:
                        key = f"{o.symbol}:{o.mode}:{o.long_exchange}:{o.short_exchange}"
                        if cooldown_ok(key):
                            await tg.send_message(cfg.TELEGRAM_CHAT_ID, format_signal(o))
                            diagnostics.signals_sent += 1
                            diagnostics.long_count[o.long_exchange] += 1
                            diagnostics.short_count[o.short_exchange] += 1
                if cycle % cfg.SUMMARY_EVERY_CYCLES == 0:
                    diagnostics.log_summary(cycle)
                    diagnostics.reset()
            except Exception as exc:
                logger.exception("scan loop error: %s", exc)
            await asyncio.sleep(cfg.SCAN_INTERVAL)


async def main() -> None:
    settings.load()
    setup_logging()

    tg = TelegramProxyManager(cfg.TELEGRAM_TOKEN, cfg.PROXY_LIST)
    await tg.initialize()
    app = await start_telegram_bot()

    loop_task = asyncio.create_task(alerts_loop(tg))
    try:
        await loop_task
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
