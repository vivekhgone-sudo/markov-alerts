"""Sends Markov Regime Engine labels (BUY / SELL / FLIP / CONT) to a Telegram chat or group.

Uses each instrument's own exchange prices (public data, no API keys):
  ETHUSD.P, BTCUSD.P  -> Delta Exchange India
  XAUUSDT.P           -> Binance USDT-M futures
runs the same rules as the TradingView indicator, and posts when a label prints on a just-closed candle.

  python mre_bot.py --minutes 58     # watch for 58 minutes (GitHub Actions starts it again every hour)
  python mre_bot.py --test           # post the latest price of every instrument and exit

Needs env vars TELEGRAM_TOKEN and TELEGRAM_CHAT_ID (a group id starts with -100...).
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from mre_engine import MRE

# ------------------------------------------------------------------ what to watch
#        name on chart   exchange   exchange symbol   timeframe
WATCH = [
    ("ETHUSD.P",  "delta",   "ETHUSD",  "5m"),
    ("BTCUSD.P",  "delta",   "BTCUSD",  "5m"),
    ("XAUUSDT.P", "binance", "XAUUSDT", "5m"),
]
# If an exchange refuses GitHub's servers (Binance blocks US-based servers), use Shark's price for that
# instrument instead and say so in the message. Set to False to skip the instrument instead.
FALLBACK_TO_SHARK = True
HISTORY = 3000                     # candles used to warm the engine up (~10 days on 5m)
CATCH_UP_MIN = 30                  # on start, also send labels from the last 30 min that were missed
STATE = Path(__file__).with_name("state.json")
STEP_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}
IST = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 markov-alerts"}


def _ms(t) -> int:
    t = int(float(t))
    return t * 1000 if t < 10**12 else t


def delta_klines(sym: str, tf: str, total: int) -> list[tuple]:
    step = STEP_MS[tf] // 1000
    end = int(time.time())
    out: dict[int, tuple] = {}
    while len(out) < total:
        start = end - step * 1500
        r = requests.get("https://api.india.delta.exchange/v2/history/candles",
                         params={"resolution": tf, "symbol": sym, "start": start, "end": end}, headers=UA, timeout=20)
        r.raise_for_status()
        res = r.json().get("result") or []
        if not res:
            break
        for k in res:
            t = _ms(k["time"])
            out[t] = (t, float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"]))
        end = start - 1
    return sorted(out.values())


def binance_klines(sym: str, tf: str, total: int) -> list[tuple]:
    out: dict[int, tuple] = {}
    end = None
    while len(out) < total:
        params = {"symbol": sym, "interval": tf, "limit": 1500}
        if end:
            params["endTime"] = end
        r = requests.get("https://fapi.binance.com/fapi/v1/klines", params=params, headers=UA, timeout=20)
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for k in data:
            out[int(k[0])] = (int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]))
        end = int(data[0][0]) - 1
        if len(data) < 1500:
            break
    return sorted(out.values())


def shark_klines(sym: str, tf: str, total: int) -> list[tuple]:
    out: dict[int, tuple] = {}
    end = None
    while len(out) < total:
        body = {"pair": sym, "interval": tf, "limit": 1500}
        if end:
            body["endTime"] = end
        r = requests.post("https://api.sharkexchange.in/v1/market/klines", json=body, headers=UA, timeout=20)
        r.raise_for_status()
        data = r.json()
        data = data.get("data", []) if isinstance(data, dict) else data
        if not data:
            break
        batch = [(_ms(k["startTime"]), float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"])) for k in data]
        for b in batch:
            out[b[0]] = b
        end = min(b[0] for b in batch) - 1
        if len(batch) < 1500:
            break
    return sorted(out.values())


SOURCES = {"delta": delta_klines, "binance": binance_klines, "shark": shark_klines}
SHARK_SYMBOL = {"ETHUSD": "ETHUSDT", "BTCUSD": "BTCUSDT", "XAUUSDT": "XAUUSDT"}


def klines(source: str, sym: str, tf: str, total: int) -> tuple[list[tuple], str]:
    """closed candles oldest-first: (open_ms, o, h, l, c), plus the exchange actually used"""
    try:
        rows, used = SOURCES[source](sym, tf, total), source
    except Exception as e:
        if not FALLBACK_TO_SHARK or source == "shark":
            raise
        print(f"{source} failed for {sym} ({e!r}); using Shark")
        rows, used = shark_klines(SHARK_SYMBOL.get(sym, sym), tf, total), "shark"
    now = int(time.time() * 1000)
    return [r for r in rows if r[0] + STEP_MS[tf] <= now][-total:], used     # drop the candle still forming


def send(text: str) -> None:
    tok, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": text}, timeout=20)
    if not r.ok:
        print("telegram error", r.status_code, r.text[:200])


def check(name: str, source: str, sym: str, tf: str, state: dict, since_ms: int) -> None:
    rows, used = klines(source, sym, tf, HISTORY)
    eng = MRE(sym, bar_ms=STEP_MS[tf])
    key = f"{name}_{tf}"
    last_sent = state.get(key, 0)
    step = STEP_MS[tf]
    for t, o, h, l, c in rows:
        lab = eng.step(t, o, h, l, c)
        close_ms = t + step
        if lab and close_ms > max(last_sent, since_ms):
            stop = l if "BUY" in lab else h
            when = datetime.fromtimestamp(close_ms / 1000, IST).strftime("%d %b %H:%M IST")
            late = (time.time() * 1000 - close_ms) / 60000
            icon = "🟢" if "BUY" in lab else "🔴"
            msg = (f"{icon} {lab}  {name}  {tf}\n"
                   f"Price: {c:g}\nStop: {stop:g}\nCandle closed: {when}")
            if used != source:
                msg += f"\n(price from {used.title()} — {source.title()} not reachable)"
            if late > 3:
                msg += f"\n(sent {late:.0f} min late)"
            send(msg)
            print("sent", key, lab, when)
            state[key] = close_ms
            last_sent = close_ms


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=58)
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    if a.test:
        lines = ["✅ Markov bot connected. Latest closed 5m candle:"]
        for name, source, sym, tf in WATCH:
            try:
                rows, used = klines(source, sym, tf, 50)
                note = "" if used == source else f" (from {used.title()}, {source.title()} not reachable)"
                lines.append(f"{name}: {rows[-1][4]:g}{note}")
            except Exception as e:
                lines.append(f"{name}: could not load prices ({type(e).__name__})")
        send("\n".join(lines))
        print("\n".join(lines))
        return
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    since = int(time.time() * 1000) - CATCH_UP_MIN * 60_000
    stop_at = time.time() + a.minutes * 60
    while True:
        for name, source, sym, tf in WATCH:
            try:
                check(name, source, sym, tf, state, since)
            except Exception as e:                        # one bad call must not stop the bot
                print("error", name, tf, repr(e))
        STATE.write_text(json.dumps(state))
        # wake 15 s after the next 5-minute candle closes
        now = time.time()
        nxt = (now // 300 + 1) * 300 + 15
        if nxt > stop_at:
            break
        time.sleep(nxt - now)


if __name__ == "__main__":
    main()
