# tg-arb — Telegram crypto arbitrage bot (production-oriented MVP)

Бот запускается как обычный Python-скрипт, сканирует реальные API Bybit/BingX/KuCoin и отправляет сигналы в Telegram.

## Что реализовано

- Режимы:
  - `spot_futures`
  - `futures_futures`
  - `funding`
  - `spread` (оба spread-режима)
  - `all`
- Реальные данные: symbols, top-of-book, funding rates.
- Расчёты:
  - gross spread
  - fees
  - net result
  - max executable size (USDT)
  - estimated PnL (USDT)
- Telegram-команды: `/start`, `/show`, `/set ...`, `/mode ...`, `/reset`.
- Доступ только для разрешённого `TELEGRAM_CHAT_ID`.
- Устойчивое чтение/сохранение `settings.json`.
- Ротация логов (`logs/arb_bot.log`, backup-файлы).
- Proxy manager с поддержкой `socks5/http/https`, fallback без прокси, автопереключение, маскирование пароля в логах.

## Структура проекта

- `futures_arb_signal.py` — основной рантайм, Telegram-команды, сканер, расчёты, отправка сигналов.
- `proxy_manager.py` — Telegram proxy failover manager.
- `config.py` — defaults + env loading (`.env`).
- `settings.json` — runtime-настройки.
- `.env.example` — пример переменных окружения.
- `requirements.txt` — зависимости.

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# отредактируйте .env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, (опционально) PROXY_LIST
```

## Запуск

```bash
python3 futures_arb_signal.py
```

## Telegram команды

- `/start`
- `/show`
- `/reset`
- `/set min_profit 0.2`
- `/set min_volume 1000`
- `/set min_funding 0.03`
- `/set min_24h_volume 500000`
- `/set symbols BTC,ETH,SOL`
- `/set symbols ALL`
- `/mode spread`
- `/mode funding`
- `/mode spot_futures`
- `/mode futures_futures`
- `/mode all`

> В режиме `funding` фильтрация идёт по `total edge` (spread edge + funding edge).  
> В режиме `spread`/`spot_futures`/`futures_futures` фильтрация идёт по `spread edge`.  
> Также применяется фильтр `min_24h_volume` (минимум 24h объёма по двум сторонам сделки).

## Пример запуска в tmux

```bash
tmux new -s tg-arb
cd /path/to/tg-arb
source .venv/bin/activate
python3 futures_arb_signal.py
# detach: Ctrl+b then d
```

## systemd service

Создайте `/etc/systemd/system/tg-arb.service`:

```ini
[Unit]
Description=Telegram Crypto Arbitrage Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/tg-arb
EnvironmentFile=/opt/tg-arb/.env
ExecStart=/opt/tg-arb/.venv/bin/python /opt/tg-arb/futures_arb_signal.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Далее:

```bash
sudo systemctl daemon-reload
sudo systemctl enable tg-arb
sudo systemctl start tg-arb
sudo systemctl status tg-arb
journalctl -u tg-arb -f
```

## Деплой на VPS (кратко)

1. Установить Python 3.10+ и `tmux`.
2. Клонировать репозиторий.
3. Создать venv и установить зависимости.
4. Заполнить `.env`.
5. Проверить `python3 futures_arb_signal.py` вручную.
6. Перенести в `systemd`.
7. Контроль логов:
   - stdout через `journalctl`
   - файловые логи: `logs/arb_bot.log` + rotation.
