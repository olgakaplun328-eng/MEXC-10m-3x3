import os
import json
import time
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# Змінні Railway: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MEXC_SYMBOLS=BTCUSDT,ETHUSDT
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_IDS = [x.strip() for x in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if x.strip()]

TIMEFRAME_SECONDS = 10 * 60
TIMEFRAME_LABEL = "10m"
DEFAULT_SYMBOLS = "BTCUSDT,ETHUSDT"

def normalize_symbol(value):
    """Приймає BTCUSDT, BTC_USDT або BTC__USDT і приводить до BTC_USDT."""
    s = value.strip().upper().replace("_", "")
    if not s:
        return ""
    if s.endswith("USDT"):
        s = s[:-4]
    return f"{s}_USDT"

SYMBOLS = list(dict.fromkeys(
    normalize_symbol(s)
    for s in os.getenv("MEXC_SYMBOLS", DEFAULT_SYMBOLS).split(",")
    if s.strip()
))
POLL_SECONDS = max(5, int(os.getenv("POLL_SECONDS", "15")))
TEST_MODE = os.getenv("TEST_MODE", "false").strip().lower() in {"1", "true", "yes", "on"}
API_URL = "https://contract.mexc.com/api/v1/contract/kline/{symbol}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("mexc-signal-bot")
SESSION = requests.Session()
SESSION.headers.update({"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": "BTC-ETH-10m-Signal-Bot/6.0"})


def utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def kyiv(ts):
    return datetime.fromtimestamp(ts, tz=ZoneInfo("Europe/Kyiv")).strftime("%Y-%m-%d %H:%M")


def symbol_label(symbol):
    return symbol.replace("_", "")


def candle_color(candle):
    if candle["close"] > candle["open"]:
        return "GREEN"
    if candle["close"] < candle["open"]:
        return "RED"
    return "DOJI"


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("Стан має бути JSON-об'єктом")
        state.setdefault("pending", [])
        state.setdefault("last_processed", None)
        return state
    except FileNotFoundError:
        return {"last_processed": None, "pending": []}
    except Exception as exc:
        log.warning("Не вдалося прочитати файл стану %s (%s). Починаємо з чистого стану", path, exc)
        return {"last_processed": None, "pending": []}


def save_state(state, path):
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


def send_telegram(text):
    if not TOKEN or not CHAT_IDS:
        log.error("TELEGRAM НЕ НАЛАШТОВАНО: додайте TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID у змінні Railway")
        return False
    success_count = 0
    for chat_id in CHAT_IDS:
        try:
            response = SESSION.post(
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=15,
            )
            payload = response.json()
            if response.ok and payload.get("ok"):
                success_count += 1
                log.info("ПОВІДОМЛЕННЯ TELEGRAM НАДІСЛАНО | чат=%s", chat_id)
            else:
                log.error("ПОМИЛКА TELEGRAM | чат=%s | статус=%s | відповідь=%s", chat_id, response.status_code, response.text[:300])
        except Exception:
            log.exception("ПОМИЛКА ПІД ЧАС НАДСИЛАННЯ TELEGRAM | чат=%s", chat_id)
    return success_count == len(CHAT_IDS)


def parse_mexc_timestamp(value):
    ts = int(float(value))
    # Часові мітки свічок MEXC зазвичай у секундах; підтримуємо також мілісекунди.
    if ts > 10_000_000_000:
        ts //= 1000
    return ts


def fetch_1m_candles(symbol):
    response = SESSION.get(
        API_URL.format(symbol=symbol),
        params={"interval": "Min1", "limit": 300, "start": int(time.time()) - 300 * 60, "end": int(time.time())},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("success") is False:
        raise RuntimeError(f"Помилка API MEXC {payload.get('code')}: {payload.get('message')}")
    data = payload.get("data")
    if not data:
        raise RuntimeError(f"MEXC повернула порожні дані: {payload}")

    candles = []
    if isinstance(data, dict) and isinstance(data.get("time"), list):
        times = data["time"]
        opens = data.get("open", [])
        closes = data.get("close", [])
        if not (len(times) == len(opens) == len(closes)):
            raise RuntimeError("Масиви свічок MEXC мають різну довжину")
        for i, ts in enumerate(times):
            candles.append({"ts": parse_mexc_timestamp(ts), "open": float(opens[i]), "close": float(closes[i])})
    elif isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                ts = row.get("time", row.get("t"))
                op = row.get("open", row.get("o"))
                cl = row.get("close", row.get("c"))
            else:
                ts, op, cl = row[0], row[1], row[2]
            candles.append({"ts": parse_mexc_timestamp(ts), "open": float(op), "close": float(cl)})
    else:
        raise RuntimeError(f"Невідомий формат відповіді MEXC: {type(data).__name__}")

    # Видаляємо дублікати та сортуємо свічки за часом.
    by_ts = {c["ts"]: c for c in candles}
    return [by_ts[k] for k in sorted(by_ts)]


def aggregate_10m(minute_candles):
    buckets = OrderedDict()
    for candle in minute_candles:
        bucket_ts = (candle["ts"] // TIMEFRAME_SECONDS) * TIMEFRAME_SECONDS
        buckets.setdefault(bucket_ts, []).append(candle)

    now = int(time.time())
    result = []
    for bucket_ts, rows in sorted(buckets.items()):
        rows.sort(key=lambda x: x["ts"])
        # Беремо лише повні закриті 10-хвилинні свічки без пропущених хвилин.
        if bucket_ts + TIMEFRAME_SECONDS <= now and len(rows) == 10:
            expected = [bucket_ts + i * 60 for i in range(10)]
            if [x["ts"] for x in rows] == expected:
                result.append({"ts": bucket_ts, "open": rows[0]["open"], "close": rows[-1]["close"], "count": 10})
    return result


def current_10m(minute_candles):
    if not minute_candles:
        return None
    bucket_ts = (minute_candles[-1]["ts"] // TIMEFRAME_SECONDS) * TIMEFRAME_SECONDS
    rows = sorted((x for x in minute_candles if (x["ts"] // TIMEFRAME_SECONDS) * TIMEFRAME_SECONDS == bucket_ts), key=lambda x: x["ts"])
    if not rows:
        return None
    return {"ts": bucket_ts, "open": rows[0]["open"], "close": rows[-1]["close"], "count": len(rows)}


class SignalEngine:
    """Патерн: свічки №1–№3 одного кольору, №4–№6 — протилежного; результат №7–№12."""
    def __init__(self, symbol):
        self.symbol = symbol
        self.state_path = os.getenv(f"STATE_FILE_{symbol}", f"state_{symbol}.json")
        self.state = load_state(self.state_path)
        self.candles = OrderedDict()
        self.pending = [p for p in self.state.get("pending", []) if isinstance(p, dict) and p.get("rule_version") == 2 and "signal_ts" in p] if isinstance(self.state.get("pending", []), list) else []
        self.pre_sent = set()
        self.initialized = False

    def seed(self, closed):
        self.candles = OrderedDict((c["ts"], c) for c in closed[-250:])
        self.candles = OrderedDict(sorted(self.candles.items()))
        # Після запуску не генеруємо сигнали заднім числом.
        self.state["last_processed"] = next(reversed(self.candles), None)
        save_state(self.state, self.state_path)
        self.initialized = True
        if self.candles:
            latest = next(reversed(self.candles.values()))
            log.info("ІНІЦІАЛІЗАЦІЯ | %s | свічок=%d | остання=%s %s", self.symbol, len(self.candles), utc(latest["ts"]), candle_color(latest))

    def _pattern_for_sixth(self, sixth_ts, live_sixth=None):
        """Перевіряє свічки №1–№6: №1–№3 одного кольору, №4–№6 — протилежного."""
        first_ts = sixth_ts - 5 * TIMEFRAME_SECONDS
        prior_ts = [first_ts + i * TIMEFRAME_SECONDS for i in range(5)]  # свічки №1–№5
        if any(ts not in self.candles for ts in prior_ts):
            return None

        first_three = [candle_color(self.candles[ts]) for ts in prior_ts[:3]]
        fourth_color = candle_color(self.candles[prior_ts[3]])
        fifth_color = candle_color(self.candles[prior_ts[4]])
        sixth = live_sixth if live_sixth is not None else self.candles.get(sixth_ts)
        if not sixth:
            return None
        sixth_color = candle_color(sixth)

        start_color = first_three[0]
        if start_color not in ("GREEN", "RED") or not all(c == start_color for c in first_three):
            return None
        opposite_color = "RED" if start_color == "GREEN" else "GREEN"
        if fourth_color != opposite_color or fifth_color != opposite_color or sixth_color != opposite_color:
            return None

        # Перша свічка патерна не повинна бути продовженням довшої серії того самого кольору.
        previous_ts = first_ts - TIMEFRAME_SECONDS
        if previous_ts in self.candles and candle_color(self.candles[previous_ts]) == start_color:
            return None

        direction = "LONG" if start_color == "GREEN" else "SHORT"
        return {
            "start_ts": first_ts,
            "signal_ts": sixth_ts,
            "start_color": start_color,
            "opposite_color": opposite_color,
            "direction": direction,
            "rule_version": 2,
            "result_colors": [],
        }

    def maybe_pre_alert(self, live_candle):
        if not self.initialized or not live_candle:
            return
        # PRE надсилається протягом останньої хвилини формування свічки №6.
        elapsed = time.time() - live_candle["ts"]
        if elapsed < TIMEFRAME_SECONDS - 60 or elapsed >= TIMEFRAME_SECONDS:
            return
        target_ts = live_candle["ts"]
        if target_ts in self.candles or target_ts in self.pre_sent:
            return
        pattern = self._pattern_for_sixth(target_ts, live_sixth=live_candle)
        if not pattern:
            return

        pat = "🔴🔴🔴🟢🟢🟢" if pattern["direction"] == "SHORT" else "🟢🟢🟢🔴🔴🔴"
        text = (
            f"⚠️ ПОПЕРЕДЖЕННЯ — {symbol_label(self.symbol)} Futures\n"
            f"Таймфрейм: {TIMEFRAME_LABEL}\n"
            f"Патерн: {pat}\n"
            f"Можливий напрямок: {pattern['direction']}\n\n"
            "Остаточний сигнал надійде лише після закриття свічки №6 і підтвердження патерна."
        )
        log.info("ПОПЕРЕДЖЕННЯ | %s | напрямок=%s | початок=%s", self.symbol, pattern["direction"], utc(pattern["start_ts"]))
        if send_telegram(text):
            self.pre_sent.add(target_ts)

    def ingest(self, closed):
        if not self.initialized:
            self.seed(closed)
            return 0
        existing = set(self.candles)
        new = [c for c in closed if c["ts"] not in existing]
        if not new:
            return 0
        for candle in sorted(new, key=lambda x: x["ts"]):
            self.candles[candle["ts"]] = candle
            self.candles = OrderedDict(sorted(self.candles.items()))
            while len(self.candles) > 250:
                self.candles.popitem(last=False)
            self.evaluate(candle["ts"])
        self.state["pending"] = self.pending
        self.state["last_processed"] = max(c["ts"] for c in new)
        save_state(self.state, self.state_path)
        return len(new)

    def evaluate(self, ts):
        keys = list(self.candles)
        idx = keys.index(ts)
        current = self.candles[ts]
        keep = []

        # Перевіряємо результат на свічках №7–№12 після сигнальної свічки №6.
        for setup in self.pending:
            rel = int((ts - setup["signal_ts"]) // TIMEFRAME_SECONDS)
            if rel < 1:
                keep.append(setup)
                continue
            if rel > 6:
                continue

            result_color = candle_color(current)
            if result_color == setup["start_color"]:
                send_telegram(
                    f"✅ ПЕРЕМОГА\n{symbol_label(self.symbol)} Futures\nНапрямок: {setup['direction']}\n"
                    f"Свічка, що підтвердила результат: №{rel + 6}/№12"
                )
                log.info("РЕЗУЛЬТАТ: ПЕРЕМОГА | %s | %s | свічка №%d", self.symbol, setup["direction"], rel + 6)
                continue

            setup.setdefault("result_colors", []).append(result_color)
            if rel == 6:
                opposite = "RED" if setup["start_color"] == "GREEN" else "GREEN"
                if len(setup["result_colors"]) == 6 and all(c == opposite for c in setup["result_colors"]):
                    send_telegram(
                        f"❌ ПОРАЗКА\n{symbol_label(self.symbol)} Futures\nНапрямок: {setup['direction']}\n"
                        "Усі свічки №7–№12 були протилежного кольору до початкової трійки."
                    )
                    log.info("РЕЗУЛЬТАТ: ПОРАЗКА | %s | %s", self.symbol, setup["direction"])
                else:
                    log.info("НЕМАЄ РЕЗУЛЬТАТУ | %s | %s | у вікні є свічка доджі", self.symbol, setup["direction"])
                continue

            # Доджі не належать ні до початкового, ні до протилежного кольору.
            keep.append(setup)
        self.pending = keep

        # Поточна закрита свічка може бути №6: перевіряємо попередні №1–№5.
        if idx < 5:
            return
        pattern = self._pattern_for_sixth(ts)
        if not pattern:
            return
        if any(p.get("signal_ts") == ts for p in self.pending):
            return

        signal = (
            f"🔔 СИГНАЛ {pattern['direction']}\n\n{symbol_label(self.symbol)} Futures\n"
            f"Таймфрейм: {TIMEFRAME_LABEL}\n"
            f"Початок патерна: {kyiv(pattern['start_ts'])} за київським часом\n\n"
            "Лише інформаційний сигнал — автоматичні угоди не відкриваються."
        )
        sent = send_telegram(signal)
        log.info("СИГНАЛ %s | %s | початок=%s | надіслано=%s", pattern["direction"], self.symbol, utc(pattern["start_ts"]), sent)
        self.pending.append(pattern)


def main():
    if not SYMBOLS:
        raise RuntimeError("Список MEXC_SYMBOLS порожній; задайте BTCUSDT,ETHUSDT")
    log.info("Запуск бота сигналів MEXC Futures | таймфрейм=%s | інтервал перевірки=%s с", TIMEFRAME_LABEL, POLL_SECONDS)
    log.info("Інструменти: %s", ", ".join(SYMBOLS))
    log.info("Telegram налаштовано: %s | кількість чатів: %d", bool(TOKEN), len(CHAT_IDS))
    if TOKEN and CHAT_IDS:
        send_telegram(f"🤖 БОТ ЗАПУЩЕНО\nMEXC Futures\nІнструменти: {', '.join(symbol_label(s) for s in SYMBOLS)}\nТаймфрейм: {TIMEFRAME_LABEL}\nЛише сповіщення — автоматичні угоди не відкриваються.")

    engines = {symbol: SignalEngine(symbol) for symbol in SYMBOLS}
    last_heartbeat = {}
    while True:
        for symbol, engine in engines.items():
            try:
                minutes = fetch_1m_candles(symbol)
                live = current_10m(minutes)
                if live:
                    engine.maybe_pre_alert(live)
                closed = aggregate_10m(minutes)
                if not closed:
                    log.warning("%s | MEXC повернула дані, але поки немає повних закритих свічок %s", symbol, TIMEFRAME_LABEL)
                    continue
                added = engine.ingest(closed)
                latest = closed[-1]
                if added:
                    log.info("НОВІ ДАНІ | %s | хвилинних свічок=%d | закритих 10-хвилинних=%d | додано=%d | остання=%s %s | відкриття=%s закриття=%s | очікують результату=%d",
                             symbol, len(minutes), len(closed), added, utc(latest["ts"]), candle_color(latest), latest["open"], latest["close"], len(engine.pending))
                elif time.time() - last_heartbeat.get(symbol, 0) >= 60:
                    age = max(0, int(time.time() - (latest["ts"] + TIMEFRAME_SECONDS)))
                    log.info("ЗВ'ЯЗОК ПРАЦЮЄ | %s | остання свічка=%s %s | вік=%s с | очікують результату=%d", symbol, utc(latest["ts"]), candle_color(latest), age, len(engine.pending))
                    last_heartbeat[symbol] = time.time()
            except Exception as exc:
                log.exception("ПОМИЛКА ЦИКЛУ | %s | %s", symbol, exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if TEST_MODE:
        # Режим перевірки конфігурації; реальні повідомлення Telegram не надсилаються.
        print("ТЕСТОВИЙ РЕЖИМ: УСПІШНО")
        print("Інструменти:", ", ".join(SYMBOLS))
        print("Таймфрейм:", TIMEFRAME_LABEL)
        print("API:", API_URL)
        print("Правило: №1–№3 одного кольору; №4–№6 протилежного; попередження в останню хвилину №6; сигнал після закриття №6; результат №7–№12")
    else:
        main()
