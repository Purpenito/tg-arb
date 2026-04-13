# tg-arb — Telegram Futures Arbitrage Terminal

Production-oriented Telegram bot for **futures-futures spread** and **futures+funding** monitoring using only real exchange APIs.

## Product concept (re-designed)

- Default mode is **manual terminal UX**: user requests scans from Telegram buttons.
- Auto-alerts are **OFF by default** and can be turned ON explicitly.
- Focused directions in v1:
  - `futures_futures` (spread)
  - `funding` (spread + funding edge)
- Real data only: symbols, depth/orderbook, funding, 24h volume.

## Supported exchanges (v1)

- Bybit
- KuCoin
- OKX
- BingX
- Bitget
- Gate

## Main commands

- `/start`
- `/show`
- `/status`
- `/reset`
- `/set capital 1000`
- `/set min_profit 0.15`
- `/set min_trade_size 500`
- `/set min_24h_volume 1000000`
- `/set min_funding 0.01`
- `/set symbols BTC,ETH,SOL`
- `/set symbols ALL`
- `/set exchanges bybit,okx,kucoin`
- `/set alerts on`
- `/set cooldown 2`
- `/set max_results 5`
- `/mode spread`
- `/mode funding`
- `/mode all`
- `/debug ETH`

## Settings model (`settings.json`)

- `capital_usdt`
- `min_profit_pct`
- `min_trade_size_usdt`
- `min_24h_volume_usdt`
- `min_funding_pct`
- `max_negative_entry_spread_pct`
- `filtered_symbols`
- `enabled_exchanges`
- `mode`
- `auto_alerts_enabled`
- `cooldown_minutes`
- `max_results`
- `only_positive_signals`
- `funding_weight`
- `log_max_mb`
- `log_backups`

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit TELEGRAM_TOKEN and TELEGRAM_CHAT_ID
```

## Run

```bash
python3 futures_arb_signal.py
```

## Run with nohup

```bash
source .venv/bin/activate
nohup python3 futures_arb_signal.py > bot.out 2>&1 &
tail -f bot.out
```

## Run with tmux

```bash
tmux new -s tg-arb
source .venv/bin/activate
python3 futures_arb_signal.py
# detach: Ctrl+b then d
```

## systemd unit

`/etc/systemd/system/tg-arb.service`

```ini
[Unit]
Description=Telegram Futures Arbitrage Terminal
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

```bash
sudo systemctl daemon-reload
sudo systemctl enable tg-arb
sudo systemctl start tg-arb
sudo systemctl status tg-arb
journalctl -u tg-arb -f
```

## VPS deployment guide (short)

1. Clone repo to VPS (`/opt/tg-arb`).
2. Create virtualenv and install requirements.
3. Configure `.env`.
4. Start once manually and verify Telegram `/start`.
5. Switch to `systemd`.
6. Monitor logs in `logs/arb_bot.log` + `journalctl`.
