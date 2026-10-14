# MEXC Futures Telegram Signal Bot — 10m

Telegram alerts only. This bot does not place trades.

## Pattern
- Candles #1–#3 must all have the same non-doji color.
- Candles #4–#6 must all have the opposite color.
- PRE-ALERT is sent during the final minute of candle #6, if candles #1–#5 are closed and the live #6 still matches the pattern.
- SIGNAL is sent only after candle #6 closes and confirms the full pattern.
- If #1–#3 are GREEN and #4–#6 are RED, direction is LONG.
- If #1–#3 are RED and #4–#6 are GREEN, direction is SHORT.
- Check candles #7–#12: any candle matching the start color (#1–#3) yields WIN.
- LOSS is sent only if all six candles #7–#12 are the opposite color to the start. A doji prevents LOSS if no WIN occurred.

## Railway setup
1. Upload `main.py`, `requirements.txt`, and this README to your project.
2. Set the Railway start command to `python main.py`.
3. Add these Railway Variables:
   - `TELEGRAM_BOT_TOKEN` — your Telegram bot token
   - `TELEGRAM_CHAT_ID` — destination chat ID (multiple IDs can be comma-separated)
   - `MEXC_SYMBOLS=BTCUSDT,ETHUSDT`
   - `POLL_SECONDS=15`
   - `TEST_MODE=false`
4. Deploy and inspect logs for `BOT ONLINE`, `INITIALIZED`, and `HEARTBEAT OK`.

## Notes
- Keep credentials private. If a bot token was shared publicly or pasted into an unsafe place, revoke it with @BotFather and update Railway Variables.
- State files are stored in the process working directory. Depending on the Railway service/storage setup, local files may not persist across redeploys.
- `TEST_MODE=true` only performs a startup/config print and exits; it does not simulate candles or send Telegram alerts.
