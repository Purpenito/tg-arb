import asyncio
import aiohttp
import time
import json
import os
from typing import Set, Optional, Dict, Any
import logging

import config as cfg
from proxy_manager import TelegramProxyManager

SETTINGS_FILE = "settings.json"
COOLDOWN_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("arb_scanner")


class Settings:
    def __init__(self):
        self.min_profit_pct = cfg.DEFAULT_MIN_PROFIT_PCT
        self.min_volume_usdt = cfg.DEFAULT_MIN_VOLUME_USDT
        self.filtered_symbols: Optional[Set[str]] = set(cfg.DEFAULT_SYMBOLS) if cfg.DEFAULT_SYMBOLS else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "min_profit_pct": self.min_profit_pct,
            "min_volume_usdt": self.min_volume_usdt,
            "filtered_symbols": list(self.filtered_symbols) if self.filtered_symbols else None,
        }

    def from_dict(self, data: Dict[str, Any]):
        self.min_profit_pct = float(data.get("min_profit_pct", cfg.DEFAULT_MIN_PROFIT_PCT))
        self.min_volume_usdt = float(data.get("min_volume_usdt", cfg.DEFAULT_MIN_VOLUME_USDT))
        syms = data.get("filtered_symbols")
        self.filtered_symbols = set(syms) if syms else None

    def reset_to_default(self):
        self.min_profit_pct = cfg.DEFAULT_MIN_PROFIT_PCT
        self.min_volume_usdt = cfg.DEFAULT_MIN_VOLUME_USDT
        self.filtered_symbols = set(cfg.DEFAULT_SYMBOLS) if cfg.DEFAULT_SYMBOLS else None

    def save_to_file(self, filename: str):
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    def load_from_file(self, filename: str):
        if os.path.exists(filename):
            try:
                with open(filename, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.from_dict(data)
                logger.info("Настройки загружены из %s", os.path.abspath(filename))
            except Exception as e:
                logger.warning("Ошибка загрузки настроек: %s. Используем значения по умолчанию.", e)
        else:
            logger.info("Файл %s не найден — создаём по умолчанию.", filename)
            self.save_to_file(filename)


settings = Settings()
last_signal_times: Dict[str, float] = {}


def signal_key(symbol: str, long_ex: str, short_ex: str) -> str:
    return f"{symbol}:{long_ex}:{short_ex}"


def on_cooldown(symbol: str, long_ex: str, short_ex: str) -> bool:
    key = signal_key(symbol, long_ex, short_ex)
    now = time.time()
    last = last_signal_times.get(key, 0)
    if now - last < COOLDOWN_SECONDS:
        return True
    last_signal_times[key] = now
    return False


def get_pair(exchange: str, base: str) -> str:
    return {
        "bybit": f"{base}USDT",
        "bingx": f"{base}-USDT",
        "kucoin": f"{base}USDTM",
    }[exchange]


def fmt_ex(exchange: str) -> str:
    return exchange.upper()


def fmt_fr_value(fr: Optional[float]) -> str:
    return f"{fr * 100:.4f}%" if fr is not None else "N/A"


def funding_side_text(rate: Optional[float], side: str) -> str:
    if rate is None:
        return "N/A"
    pct = abs(rate) * 100
    if rate > 0:
        return f"{side} {'платит' if side == 'LONG' else 'получает'} {pct:.4f}%"
    if rate < 0:
        return f"{side} {'получает' if side == 'LONG' else 'платит'} {pct:.4f}%"
    return f"{side} funding 0.0000%"


async def start_telegram_bot():
    from telegram.ext import Application, CommandHandler, ContextTypes
    from telegram import Update

    allowed_id = str(cfg.TELEGRAM_CHAT_ID)
    proxy_url = cfg.PROXY_LIST[0] if cfg.PROXY_LIST else None

    async def auth_check(update: Update) -> bool:
        user_id = str(update.effective_user.id) if update.effective_user else ""
        username = update.effective_user.username if update.effective_user else None
        logger.info("Команда от user_id=%s username=@%s", user_id, username)
        if user_id != allowed_id:
            if update.message:
                await update.message.reply_text("🔒 У вас нет доступа к этому боту.")
            return False
        return True

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await auth_check(update):
            return
        syms = ", ".join(sorted(settings.filtered_symbols)) if settings.filtered_symbols else "ВСЕ доступные"
        msg = (
            "🚀 <b>Арбитражный сканер запущен!</b>\n"
            "Поддерживаемые биржи: <b>BYBIT + BINGX + KUCOIN</b>\n"
            "Логика позиции: <b>LONG на дешёвой бирже, SHORT на дорогой</b>\n"
            "Funding: <b>при положительном funding LONG платит SHORT, при отрицательном SHORT платит LONG</b>\n"
            f"📉 Мин. прибыль: <code>{settings.min_profit_pct:.3f}%</code>\n"
            f"💵 Мин. объём: <code>${settings.min_volume_usdt:,.0f}</code>\n"
            f"🌕 Монеты: <code>{syms}</code>\n\n"
            "Команды:\n"
            "<code>/show</code>\n"
            "<code>/set min_profit 0.15</code>\n"
            "<code>/set min_volume 5000</code>\n"
            "<code>/set symbols BTC,ETH,SOL</code>\n"
            "<code>/set symbols ALL</code>\n"
            "<code>/reset</code>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await auth_check(update):
            return
        syms = ", ".join(sorted(settings.filtered_symbols)) if settings.filtered_symbols else "ВСЕ доступные"
        msg = (
            "<b>Текущие настройки:</b>\n"
            f"• Мин. прибыль: <code>{settings.min_profit_pct:.3f}%</code>\n"
            f"• Мин. объём: <code>${settings.min_volume_usdt:,.0f} USDT</code>\n"
            f"• Монеты: <code>{syms}</code>\n"
            "• Биржи: <code>BYBIT, BINGX, KUCOIN</code>\n"
            "• Направления: <code>LONG / SHORT</code>"
        )
        await update.message.reply_text(msg, parse_mode="HTML")

    async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await auth_check(update):
            return
        settings.reset_to_default()
        settings.filtered_symbols = None
        settings.save_to_file(SETTINGS_FILE)
        await update.message.reply_text("🔄 Настройки сброшены. Теперь ищутся ВСЕ доступные монеты.")

    async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await auth_check(update):
            return

        text = update.message.text or ""
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text(
                "🛠 Использование:\n"
                "/set min_profit 0.2\n"
                "/set min_volume 10000\n"
                "/set symbols BTC,ETH,SOL\n"
                "/set symbols ALL"
            )
            return

        _, key, value = parts
        key = key.lower().strip()
        value = value.strip()

        try:
            if key == "min_profit":
                settings.min_profit_pct = float(value)
                msg = f"✅ min_profit = {settings.min_profit_pct}%"
            elif key == "min_volume":
                settings.min_volume_usdt = float(value)
                msg = f"✅ min_volume = ${settings.min_volume_usdt:,.0f} USDT"
            elif key == "symbols":
                if value.upper() in {"ALL", "ВСЕ", "*"}:
                    settings.filtered_symbols = None
                    msg = "✅ Символы: ВСЕ доступные"
                else:
                    syms = [s.strip().upper() for s in value.split(",") if s.strip()]
                    settings.filtered_symbols = set(syms) if syms else None
                    msg = f"✅ Символы: {', '.join(sorted(settings.filtered_symbols)) if settings.filtered_symbols else 'ВСЕ доступные'}"
            else:
                msg = "❌ Неизвестный параметр. Используй: min_profit, min_volume, symbols"

            settings.save_to_file(SETTINGS_FILE)
            await update.message.reply_text(msg)
        except ValueError as e:
            logger.error("Ошибка в /set: %s", e)
            await update.message.reply_text(
                "🛠 Использование:\n"
                "/set min_profit 0.2\n"
                "/set min_volume 10000\n"
                "/set symbols BTC,ETH,SOL\n"
                "/set symbols ALL"
            )

    builder = Application.builder().token(cfg.TELEGRAM_TOKEN)
    if proxy_url:
        builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)

    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("reset", cmd_reset))

    logger.info("Запуск Telegram-бота в режиме polling")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    return app


async def get_all_bybit_symbols(session: aiohttp.ClientSession) -> Set[str]:
    url = "https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000"
    symbols: Set[str] = set()
    try:
        async with session.get(url) as resp:
            data = await resp.json()
            if data.get("retCode") == 0:
                for item in data["result"]["list"]:
                    if (
                        item.get("quoteCoin") == "USDT"
                        and item.get("contractType") == "LinearPerpetual"
                        and item.get("status") == "Trading"
                    ):
                        symbols.add(item["baseCoin"])
    except Exception as e:
        logger.warning("[Bybit] Ошибка списка символов: %s", e)
    return symbols


async def get_all_bingx_symbols(session: aiohttp.ClientSession) -> Set[str]:
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/contracts"
    symbols: Set[str] = set()
    try:
        async with session.get(url) as resp:
            data = await resp.json()
            if data.get("code") == "0":
                for item in data.get("data", []):
                    sym = item.get("symbol", "")
                    if sym.endswith("-USDT") and item.get("status") == 1:
                        symbols.add(sym.replace("-USDT", ""))
    except Exception as e:
        logger.warning("[BingX] Ошибка списка символов: %s", e)
    return symbols


async def get_all_kucoin_symbols(session: aiohttp.ClientSession) -> Set[str]:
    url = "https://api-futures.kucoin.com/api/v1/contracts/active"
    symbols: Set[str] = set()
    try:
        async with session.get(url) as resp:
            data = await resp.json()
            if data.get("code") == "200000":
                for item in data.get("data", []):
                    if (
                        not item.get("isInverse")
                        and item.get("settleCurrency") == "USDT"
                        and item.get("status") == "Open"
                    ):
                        base = item.get("baseCurrency", "")
                        if base:
                            symbols.add(base)
    except Exception as e:
        logger.warning("[KuCoin] Ошибка списка символов: %s", e)
    return symbols


async def update_symbol_map(session: aiohttp.ClientSession) -> Dict[str, Set[str]]:
    bybit, bingx, kucoin = await asyncio.gather(
        get_all_bybit_symbols(session),
        get_all_bingx_symbols(session),
        get_all_kucoin_symbols(session),
    )
    return {"bybit": bybit, "bingx": bingx, "kucoin": kucoin}


async def fetch_orderbook(session: aiohttp.ClientSession, exchange: str, symbol: str):
    pair = get_pair(exchange, symbol)
    timeout = aiohttp.ClientTimeout(total=8)

    try:
        if exchange == "bybit":
            url = f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={pair}&limit=1"
            async with session.get(url, timeout=timeout) as resp:
                data = await resp.json()
                if data.get("retCode") == 0:
                    b = data["result"]
                    if b.get("b") and b.get("a"):
                        return {
                            "bid": float(b["b"][0][0]),
                            "ask": float(b["a"][0][0]),
                            "bid_qty": float(b["b"][0][1]),
                            "ask_qty": float(b["a"][0][1]),
                        }

        elif exchange == "bingx":
            url = f"https://open-api.bingx.com/openApi/swap/v2/quote/bookTicker?symbol={pair}"
            async with session.get(url, timeout=timeout) as resp:
                data = await resp.json()
                if data.get("code") == "0" and data.get("data", {}).get("book_ticker"):
                    b = data["data"]["book_ticker"]
                    return {
                        "bid": float(b["bid_price"]),
                        "ask": float(b["ask_price"]),
                        "bid_qty": float(b["bid_qty"]),
                        "ask_qty": float(b["ask_qty"]),
                    }

        elif exchange == "kucoin":
            url = f"https://api-futures.kucoin.com/api/v1/level2/snapshot?symbol={pair}"
            async with session.get(url, timeout=timeout) as resp:
                data = await resp.json()
                if data.get("code") == "200000" and data.get("data"):
                    d = data["data"]
                    bids = d.get("bids") or []
                    asks = d.get("asks") or []
                    if bids and asks:
                        return {
                            "bid": float(bids[0][0]),
                            "ask": float(asks[0][0]),
                            "bid_qty": float(bids[0][1]),
                            "ask_qty": float(asks[0][1]),
                        }
    except asyncio.TimeoutError:
        logger.warning("TIMEOUT orderbook %s %s", exchange, symbol)
    except Exception as e:
        logger.warning("ERROR orderbook %s %s: %s", exchange, symbol, e)

    return None


async def fetch_funding_rate(session: aiohttp.ClientSession, exchange: str, symbol: str):
    pair = get_pair(exchange, symbol)
    timeout = aiohttp.ClientTimeout(total=8)

    try:
        if exchange == "bybit":
            url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={pair}"
            async with session.get(url, timeout=timeout) as resp:
                data = await resp.json()
                if data.get("retCode") == 0 and data["result"]["list"]:
                    return float(data["result"]["list"][0]["fundingRate"])

        elif exchange == "bingx":
            url = f"https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex?symbol={pair}"
            async with session.get(url, timeout=timeout) as resp:
                data = await resp.json()
                if data.get("code") == "0" and data.get("data"):
                    return float(data["data"]["lastFundingRate"])

        elif exchange == "kucoin":
            url = f"https://api-futures.kucoin.com/api/v1/contracts/{pair}"
            async with session.get(url, timeout=timeout) as resp:
                data = await resp.json()
                if data.get("code") == "200000" and data.get("data"):
                    value = data["data"].get("fundingFeeRate")
                    return float(value) if value is not None else None
    except Exception as e:
        logger.warning("ERROR funding %s %s: %s", exchange, symbol, e)

    return None


async def scan_symbol(session: aiohttp.ClientSession, tg: TelegramProxyManager, symbol: str, available_on: Set[str]):
    exchanges = [ex for ex in ["bybit", "bingx", "kucoin"] if ex in available_on]
    results = await asyncio.gather(*[fetch_orderbook(session, ex, symbol) for ex in exchanges])

    books = {}
    for ex, result in zip(exchanges, results):
        if result:
            books[ex] = result

    if len(books) < 2:
        logger.info("CUT %s: books<2 books=%s", symbol, list(books.keys()))
        return

    long_ex = min(books, key=lambda e: books[e]["ask"])
    short_ex = max(books, key=lambda e: books[e]["bid"])

    if long_ex == short_ex:
        logger.info("CUT %s: same_exchange=%s", symbol, long_ex)
        return

    long_price = books[long_ex]["ask"]
    short_price = books[short_ex]["bid"]
    long_qty = books[long_ex]["ask_qty"]
    short_qty = books[short_ex]["bid_qty"]

    long_vol_usdt = long_price * long_qty
    short_vol_usdt = short_price * short_qty

    if long_vol_usdt < settings.min_volume_usdt or short_vol_usdt < settings.min_volume_usdt:
        logger.info(
            "CUT %s: volume long_vol=%.2f short_vol=%.2f min=%.2f",
            symbol,
            long_vol_usdt,
            short_vol_usdt,
            settings.min_volume_usdt,
        )
        return

    if long_price <= 0 or short_price <= long_price:
        logger.info("CUT %s: price long=%.6f short=%.6f", symbol, long_price, short_price)
        return

    fee_long = cfg.FEES.get(long_ex, 0.001)
    fee_short = cfg.FEES.get(short_ex, 0.001)
    total_fee_pct = (fee_long + fee_short) * 100
    gross_pct = (short_price - long_price) / long_price * 100
    net_pct = gross_pct - total_fee_pct

    logger.info(
        "DEBUG %s: long=%s %.6f short=%s %.6f gross=%.4f%% net=%.4f%% long_vol=%.2f short_vol=%.2f",
        symbol,
        long_ex,
        long_price,
        short_ex,
        short_price,
        gross_pct,
        net_pct,
        long_vol_usdt,
        short_vol_usdt,
    )

    if net_pct < settings.min_profit_pct:
        logger.info("CUT %s: net_pct=%.4f min_profit=%.4f", symbol, net_pct, settings.min_profit_pct)
        return

    if on_cooldown(symbol, long_ex, short_ex):
        logger.info("CUT %s: cooldown %s->%s", symbol, long_ex, short_ex)
        return

    fr_long, fr_short = await asyncio.gather(
        fetch_funding_rate(session, long_ex, symbol),
        fetch_funding_rate(session, short_ex, symbol),
    )

    net_funding = None
    if fr_long is not None and fr_short is not None:
        net_funding = fr_short - fr_long

    if net_funding is None:
        funding_comment = "⚪ Funding: N/A"
    elif net_funding > 0:
        funding_comment = f"💚 По funding позиция в плюс: +{net_funding * 100:.4f}%"
    elif net_funding < 0:
        funding_comment = f"🔴 По funding позиция в минус: {net_funding * 100:.4f}%"
    else:
        funding_comment = "⚪ Funding нейтральный"

    msg = (
        f"💎 <b>АРБИТРАЖ СИГНАЛ</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>{symbol}/USDT</b>\n\n"
        f"🟢 <b>LONG</b> → {fmt_ex(long_ex)}\n"
        f"   └ Ask:     <code>${long_price:.6f}</code>\n"
        f"   └ Объём:   <code>{long_qty:.4f}</code> (${long_vol_usdt:,.0f})\n"
        f"   └ Funding: <code>{fmt_fr_value(fr_long)}</code>\n"
        f"   └ {funding_side_text(fr_long, 'LONG')}\n\n"
        f"🔴 <b>SHORT</b> → {fmt_ex(short_ex)}\n"
        f"   └ Bid:     <code>${short_price:.6f}</code>\n"
        f"   └ Объём:   <code>{short_qty:.4f}</code> (${short_vol_usdt:,.0f})\n"
        f"   └ Funding: <code>{fmt_fr_value(fr_short)}</code>\n"
        f"   └ {funding_side_text(fr_short, 'SHORT')}\n\n"
        f"📊 <b>P&L:</b>\n"
        f"   └ Грубая прибыль:  <code>{gross_pct:.3f}%</code>\n"
        f"   └ Комиссии:        <code>-{total_fee_pct:.3f}%</code>\n"
        f"   └ ✅ Чистая:        <b>{net_pct:.3f}%</b>\n"
        f"   └ {funding_comment}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🕒 {time.strftime('%H:%M:%S')} UTC"
    )

    logger.info("SIGNAL %s | LONG %s -> SHORT %s | net=%.3f%%", symbol, fmt_ex(long_ex), fmt_ex(short_ex), net_pct)
    await tg.send_message(cfg.TELEGRAM_CHAT_ID, msg)


async def main():
    settings.load_from_file(SETTINGS_FILE)

    tg = TelegramProxyManager(cfg.TELEGRAM_TOKEN, cfg.PROXY_LIST)
    await tg.initialize()

    bot_app = await start_telegram_bot()

    connector = aiohttp.TCPConnector(limit=30)
    async with aiohttp.ClientSession(connector=connector) as session:
        symbol_map = await update_symbol_map(session)
        last_update = time.time()

        eligible = sorted({
            sym
            for sym in (symbol_map["bybit"] | symbol_map["bingx"] | symbol_map["kucoin"])
            if sum(sym in s for s in symbol_map.values()) >= 2
        })

        shown_syms = ", ".join(sorted(settings.filtered_symbols)) if settings.filtered_symbols else "ВСЕ доступные"
        await tg.send_message(
            cfg.TELEGRAM_CHAT_ID,
            f"🚀 <b>Арбитражный сканер запущен!</b>\n"
            f"📦 Монет доступно (>=2 биржи): {len(eligible)}\n"
            f"📉 Мин. прибыль: {settings.min_profit_pct:.3f}%\n"
            f"💵 Мин. объём: ${settings.min_volume_usdt:,.0f}\n"
            f"🌕 Монеты: {shown_syms}\n"
            f"🏦 Биржи: BYBIT + BINGX + KUCOIN\n"
            f"📌 Логика: LONG на дешёвой бирже, SHORT на дорогой"
        )

        try:
            while True:
                if time.time() - last_update > getattr(cfg, "UPDATE_PAIRS_INTERVAL", 1800):
                    symbol_map = await update_symbol_map(session)
                    last_update = time.time()
                    eligible = sorted({
                        sym
                        for sym in (symbol_map["bybit"] | symbol_map["bingx"] | symbol_map["kucoin"])
                        if sum(sym in s for s in symbol_map.values()) >= 2
                    })
                    logger.info("Обновлён список монет: %s", len(eligible))

                if settings.filtered_symbols:
                    symbols_to_scan = [s for s in sorted(settings.filtered_symbols) if s in eligible]
                else:
                    symbols_to_scan = eligible

                logger.info("LOOP symbols=%s", len(symbols_to_scan))

                tasks = []
                for sym in symbols_to_scan:
                    available_on = {ex for ex, syms in symbol_map.items() if sym in syms}
                    if len(available_on) >= 2:
                        tasks.append(scan_symbol(session, tg, sym, available_on))

                for i in range(0, len(tasks), cfg.BATCH_SIZE):
                    batch = tasks[i:i + cfg.BATCH_SIZE]
                    await asyncio.gather(*batch)
                    await asyncio.sleep(1.0)

                await asyncio.sleep(cfg.SCAN_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Сканер остановлен вручную")
        finally:
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Сканер остановлен вручную")
