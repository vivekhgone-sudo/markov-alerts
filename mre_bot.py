"""R&V HFMS v7 — Telegram bot. Same rules as the TradingView indicator "Raghu&Vivek Hedge fund Management system v7".

Posts every instruction with EXACT quantities for BTC, ETH and gold (5-minute candles):
orders, skips, fills + stop orders, trail moves, BTC partial profits, ETH add-on / join, holds, exits,
re-entries and Unger trades.

  python mre_bot.py --minutes 58     # watch for 58 minutes (GitHub Actions starts it again every hour)
  python mre_bot.py --test           # post prices, today's settings and each coin's current status, then exit
  python mre_bot.py --dry            # print the messages here instead of sending them

Telegram: env TELEGRAM_TOKEN and TELEGRAM_CHAT_ID (GitHub secrets — unchanged).
Account:  GitHub repository variables ACCOUNT_BALANCE and HIGHEST_BALANCE (or edit SETTINGS below).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import requests

from mre_engine import MRE

# ═════════════════════════════════════════════════════════════════ SETTINGS (repository variables override these)
SETTINGS = {
    "ACCOUNT_BALANCE": 50000.0,     # ₹ — update whenever it changes
    "HIGHEST_BALANCE": 50000.0,     # ₹ — highest balance reached so far; only ever raise it
    "BASE_RISK_PCT": 0.5,           # % of balance per normal trade at a new high (shrinks automatically below the high)
    "USD_INR": 105.0,                # ₹ per USDT
    "LEVERAGE_MAX": 40.0,
    "FED_DATES": "2026-10-28,2026-12-09",   # New York dates of Fed announcements
}
#        chart name    exchange  symbol     coin
WATCH = [
    ("ETHUSD.P",  "delta", "ETHUSD",  "ETH"),
    ("BTCUSD.P",  "delta", "BTCUSD",  "BTC"),
    ("XAUUSDT.P", "shark", "XAUUSDT", "XAU"),     # Binance blocks GitHub's servers, so Shark's gold price is used
]
BTC_DAILY = ("delta", "BTCUSD")                   # BTC 200-day check
# ═════════════════════════════════════════════════════════════════ tested rules — same as the indicator
HISTORY_DAYS = 30
CATCH_UP_MIN = 30
TF = 300_000
DAY = 86_400_000
HOUR = 3_600_000
STATE = Path(__file__).with_name("state.json")
NY = ZoneInfo("America/New_York")
IST = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 markov-alerts"}
NAME = {"BTC": "BTC", "ETH": "ETH", "XAU": "GOLD"}
REF = {"BTC": 3.71, "ETH": 4.90, "XAU": 1.16}                       # typical daily range % 2019-2026
BUF = {"BTC": 0.20, "ETH": 0.30, "XAU": 0.30}                       # FLIP/CONT buffer %
SL = {"BTC": (1.0, 1.0), "ETH": (2.08, 0.83), "XAU": (0.75, 0.75)}  # stop % (BUY/SELL, FLIP/CONT)
BE = {"BTC": 1.5, "ETH": 1.5, "XAU": 2.0}                           # stop to entry at (R), then every 1R
TP = {"BTC": (2.0, 5.0), "ETH": (0.0, 0.0), "XAU": (0.0, 0.0)}      # BTC books 50% at +2%, 30% at +5%
REON = {"BTC": True, "ETH": True, "XAU": False}
STEP_R = 1.0; XAU_BIG = 2.5; JOIN_R = 1.5; ADD_AT = 1.5; ADD_Q = 0.33; OFF = 0.02; LIM_WAIT = 60
RE_HOURS = 24; LINE_HOURS = 24; MAX_FLIPS = 1
UNG_MULT = 3.0; UNG_STOP = 1.0; UNG_FROM = 10; UNG_TO = 14; UNG_EXIT_H = 2
CCI_LEN = 27; CCI_LOOK = 18; CCI_LVL = -100.0
SZ_MIN = 0.25; SZ_MAX = 3.0; SHRINK_AT = 30.0; FLOOR = 25.0


def cfg(key):
    v = os.environ.get(key)
    if v in (None, ""):
        return SETTINGS[key]
    return v if isinstance(SETTINGS[key], str) else float(v)


# ═════════════════════════════════════════════════════════════════ prices (closed candles, oldest first)
def _ms(t) -> int:
    t = int(float(t))
    return t * 1000 if t < 10**12 else t


def delta_klines(sym: str, res: str, start_ms: int) -> dict:
    step = TF if res == "5m" else DAY
    out, s = {}, start_ms // 1000
    now = int(time.time())
    while s < now:
        e = min(now, s + step // 1000 * 2000)
        r = requests.get("https://api.india.delta.exchange/v2/history/candles",
                         params={"resolution": res, "symbol": sym, "start": s, "end": e}, headers=UA, timeout=20)
        r.raise_for_status()
        for k in r.json().get("result") or []:
            t = _ms(k["time"])
            out[t] = (t, float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"]))
        s = e + 1
    return out


def shark_klines(sym: str, res: str, start_ms: int) -> dict:
    out, end = {}, None
    for _ in range(40):
        body = {"pair": sym, "interval": res, "limit": 1500}
        if end:
            body["endTime"] = end
        r = requests.post("https://api.sharkexchange.in/v1/market/klines", json=body, headers=UA, timeout=20)
        r.raise_for_status()
        data = r.json()
        data = data.get("data", []) if isinstance(data, dict) else data
        if not data:
            break
        for k in data:
            t = _ms(k["startTime"])
            out[t] = (t, float(k["open"]), float(k["high"]), float(k["low"]), float(k["close"]))
        oldest = min(_ms(k["startTime"]) for k in data)
        if oldest <= start_ms or (end and oldest >= end):
            break
        end = oldest - 1
    return out


def klines(src: str, sym: str, res: str, start_ms: int) -> dict:
    rows = (delta_klines if src == "delta" else shark_klines)(sym, res, start_ms)
    step = TF if res == "5m" else DAY
    now = int(time.time() * 1000)
    return {t: r for t, r in rows.items() if start_ms <= t and t + step <= now}     # drop the candle still forming


# ═════════════════════════════════════════════════════════════════ daily context (previous days only, like [1])
def daily_context(rows: list) -> dict:
    out, rng, closes = {}, [], []
    e50 = e200 = None

    def ctx():
        med = lambda n: median(rng[-n:]) if len(rng) >= n else None
        s200 = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
        last = closes[-1] if closes else None
        return {"m30": med(30), "m10": med(10), "m60": med(60),
                "a50": None if e50 is None else (1.0 if last > e50 else -1.0),
                "a200": None if e200 is None else (1.0 if last > e200 else -1.0),
                "up200": None if s200 is None else last > s200}

    for t, o, h, l, c in rows:
        out[t // DAY] = ctx()
        rng.append((h - l) / c * 100 if c > 0 else 0.0)
        e50 = c if e50 is None else e50 + 2 / 51 * (c - e50)
        e200 = c if e200 is None else e200 + 2 / 201 * (c - e200)
        closes.append(c)
    if rows:
        out[rows[-1][0] // DAY + 1] = ctx()          # today (its own candle is still forming)
    return out


# ═════════════════════════════════════════════════════════════════ helpers
def qs(q): return f"{q:.3f}"
def r3(q): return round(q * 1000) / 1000.0
def pts(v): return ("+" if v >= 0 else "") + f"{v:,.1f}"
def hhmm(ms): return datetime.fromtimestamp(ms / 1000, IST).strftime("%H:%M")
def when(ms): return datetime.fromtimestamp(ms / 1000, IST).strftime("%d %b %H:%M IST")
def px(v, coin): return f"{v:,.1f}" if coin == "BTC" else f"{v:,.2f}"


def risk_pct():
    bal, peak = cfg("ACCOUNT_BALANCE"), cfg("HIGHEST_BALANCE")
    dd = 1 - bal / peak if peak > bal else 0.0
    return cfg("BASE_RISK_PCT") * max(FLOOR / 100, 1 - dd / (SHRINK_AT / 100)), dd


def qty_for(dist, mult):
    q = cfg("ACCOUNT_BALANCE") * risk_pct()[0] / 100 * mult / (dist * cfg("USD_INR")) if dist > 0 else 0.0
    return max(0.001, round(q * 1000) / 1000.0)


def lev_for(stop_pct):
    return int(max(1, min(cfg("LEVERAGE_MAX"), math.floor(50 / max(stop_pct, 0.01) / 5) * 5)))


# ═════════════════════════════════════════════════════════════════ the v7 system for one coin
def run_coin(coin: str, rows: list, dctx: dict, bctx: dict):
    """Replays the candles exactly like the indicator. Returns (messages [(candle close ms, text)], final state)."""
    crypto = coin in ("BTC", "ETH")
    nm = NAME[coin]
    eng = MRE(coin + ("USDT" if coin == "XAU" else "USD"), bar_ms=TF, weekend_mode="Off")
    th0 = eng.th
    fed = set(x.strip() for x in str(cfg("FED_DATES")).split(",") if x.strip())
    n = len(rows)
    tp = [(r[2] + r[3] + r[4]) / 3 for r in rows]
    cci = [None] * n
    for i in range(CCI_LEN - 1, n):
        w = tp[i - CCI_LEN + 1:i + 1]
        m = sum(w) / CCI_LEN
        md = sum(abs(x - m) for x in w) / CCI_LEN
        cci[i] = (tp[i] - m) / (0.015 * md) if md > 0 else 0.0
    hour_hi = {}
    for r in rows:
        k = r[0] // HOUR
        hour_hi[k] = max(hour_hi.get(k, r[2]), r[2])

    ev = []
    S = dict(pos=0, paper=False, en=None, stp=None, R0=None, scE=1.0, kE=1.0, joinPx=None, joinAdj=0.0, added=False,
             addPx=None, sizeM=1.0, sWhy="", oSize=1.0, oWhy="", mkDir=0, uPos=0, uEn=None, uSt=None, uExit=None,
             uDone=None, uQty=0.0, oQty=0.0, oDist=None, oLev=40, tQty=0.0, tLev=40, lastPct=0.0, best=0.0, rem=1.0,
             realized=0.0, g1=False, g2=False, pend=0, pendTyp=0, limDir=0, limTyp=0, limPx=None, limRef=None,
             limName="", limExp=None, pendRef=None, pendRe=False, pendName="", sigClose=None, sigT=None, sigSide=0,
             reArm=False, reUsed=0, exitBar=-1, held=0.0, hasLast=False, lastRes=0.0, lastSide=0, volK=1.0, posName="")
    hi_lvl = lo_lvl = cand = lvl_time = None
    lvl_dir = 0; flipped = False; n_flips = 0

    for i, (t, o, h, l, c) in enumerate(rows):
        close_ms = t + TF
        dc = dctx.get(t // DAY, {}); bc = bctx.get(t // DAY, {})
        volK = max(0.5, min(2.0, dc["m30"] / REF[coin])) if dc.get("m30") else 1.0
        S["volK"] = volK
        calm = True if (dc.get("m10") is None or dc.get("m60") is None) else dc["m10"] <= dc["m60"]
        a50, a200 = dc.get("a50"), dc.get("a200")
        btc_up = True if bc.get("up200") is None else bc["up200"]
        w = [x for x in cci[max(0, i - CCI_LOOK):i] if x is not None]
        cdip = len(w) == CCI_LOOK and min(w) < CCI_LVL
        nyt = datetime.fromtimestamp(t / 1000, NY)
        fed_day = nyt.strftime("%Y-%m-%d") in fed
        fedT = int(datetime(nyt.year, nyt.month, nyt.day, 14, 0, tzinfo=NY).timestamp() * 1000)
        s_key = datetime.fromtimestamp((t - 17 * HOUR) / 1000, NY).strftime("%Y%m%d")
        ung_win = UNG_FROM <= nyt.hour <= UNG_TO
        ung_ok = (coin == "XAU" or btc_up) and cdip
        ung_lvl = hour_hi.get(t // HOUR - 1)

        def sl_for(typ):
            return (SL[coin][0] if typ == 1 else SL[coin][1]) * volK

        def size_for(d):
            w50 = a50 is not None and a50 * d > 0
            w200 = a200 is not None and a200 * d > 0
            m = ((1.25 if w50 else 0.75) if crypto else 1.0) * (1.0 if w50 else 0.5) * (1.5 if w200 else 1.0) * (1.0 if calm else 0.5) / 0.714
            why = ("EMA50+ " if w50 else "EMA50- ") + ("EMA200+ " if w200 else "EMA200- ") + ("calm+" if calm else "volatile-")
            if fed_day and ((crypto and t < fedT) or (coin == "XAU" and t >= fedT + 2 * HOUR)):
                m *= 0.5; why += " Fed-"
            return max(SZ_MIN, min(SZ_MAX, m)), why

        def will_skip(d):
            return S["hasLast"] and (S["lastPct"] >= XAU_BIG * volK if coin == "XAU" else (S["lastRes"] > 0.0001 and d != S["lastSide"]))

        def skip_why():
            return f"after a big win ({XAU_BIG:g}%+)" if coin == "XAU" else "no opposite trade after a win"

        def open_qty():
            return S["held"]                       # coins you actually hold right now

        def add_pl(price):
            return ADD_Q * (price - S["addPx"]) * S["pos"] if S["added"] else 0.0

        def say(text):
            ev.append((close_ms, text))

        def side(d): return "BUY" if d == 1 else "SELL"

        # ---------- Markov BUY / SELL (threshold follows volatility)
        eng.th = th0 * volK
        lab = eng.step(t, o, h, l, c)
        buy, sell = lab == "BUY", lab == "SELL"
        # ---------- FLIP / CONT
        flip_buy = flip_sell = cont_buy = cont_sell = False
        buf = c * BUF[coin] * volK / 100.0
        if hi_lvl is not None and not (buy or sell):
            if lvl_dir == 1:
                if not flipped:
                    if n_flips >= MAX_FLIPS: cand = None
                    elif cand is None:
                        if c < lo_lvl - buf: cand = l
                    elif c < cand - buf: flip_sell = True; n_flips += 1; flipped = True; cand = None
                    elif c > hi_lvl + buf: cand = None
                elif c > hi_lvl + buf: cont_buy = True; flipped = False
            elif lvl_dir == -1:
                if not flipped:
                    if n_flips >= MAX_FLIPS: cand = None
                    elif cand is None:
                        if c > hi_lvl + buf: cand = h
                    elif c > cand + buf: flip_buy = True; n_flips += 1; flipped = True; cand = None
                    elif c < lo_lvl - buf: cand = None
                elif c < lo_lvl - buf: cont_sell = True; flipped = False
        if buy or sell:
            hi_lvl, lo_lvl, cand, flipped, n_flips, lvl_dir, lvl_time = h, l, None, False, 0, (1 if buy else -1), t
        elif hi_lvl is not None and t - lvl_time >= LINE_HOURS * HOUR:
            hi_lvl = lo_lvl = cand = None
        sig_dir = 1 if (buy or flip_buy or cont_buy) else -1 if (sell or flip_sell or cont_sell) else 0
        sig_typ = 1 if (buy or sell) else 2 if (flip_buy or flip_sell) else 3 if (cont_buy or cont_sell) else 0
        sig_name = ("BUY" if buy else "SELL" if sell else "FLIP BUY" if flip_buy else "FLIP SELL" if flip_sell
                    else "CONT BUY" if cont_buy else "CONT SELL" if cont_sell else "")

        # ---------- 1) entry: re-entry at this open, or the waiting stop order if price reached it
        e_dir = 0; e0 = s0 = None; scN = 1.0; e_name = ""
        if S["pend"] != 0:
            e_dir = S["pend"]; e0 = o; scN = e0 / 100.0
            slp = sl_for(S["pendTyp"]) * scN
            s0 = e0 - e_dir * S["R0"] if S["pendRe"] else (e0 - e_dir * slp if slp > 0 else S["pendRef"])
            e_name = S["pendName"]; S["pend"] = 0
        elif S["limDir"] != 0 and S["pos"] == 0:
            d = S["limDir"]
            if (h >= S["limPx"]) if d == 1 else (l <= S["limPx"]):
                e_dir = d; e0 = max(S["limPx"], o) if d == 1 else min(S["limPx"], o); scN = e0 / 100.0
                sll = sl_for(S["limTyp"]) * scN
                s0 = e0 - d * sll if sll > 0 else S["limRef"]
                e_name = S["limName"]; S["limDir"] = 0
            elif t >= S["limExp"]:
                S["limDir"] = 0
                if will_skip(d):
                    say(f"⚪ {nm}: skipped order expired (price never reached {px(S['limPx'], coin)}). Nothing to do.")
                else:
                    say(f"⚪ {nm}: CANCEL your {side(d)} {qs(S['oQty'])} {nm} stop order at {px(S['limPx'], coin)} — "
                        f"not triggered within {LIM_WAIT} min. No trade.")
        if e_dir != 0 and (e0 - s0) * e_dir > 0:
            S["paper"] = will_skip(e_dir)
            S.update(pos=e_dir, posName=e_name, en=e0, stp=s0, R0=(e0 - s0) * e_dir, scE=scN, kE=volK, joinPx=None,
                     joinAdj=0.0, added=False, addPx=None, sizeM=S["oSize"], tQty=S["oQty"], held=S["oQty"], tLev=S["oLev"],
                     sWhy=S["oWhy"], best=0.0, rem=1.0, realized=0.0, g1=False, g2=False)
            if S["paper"]:
                jl = e0 + e_dir * JOIN_R * S["R0"]
                say(f"⚪ {nm} SKIPPED {e_name} ({skip_why()}) — do NOT enter. Followed on paper until it ends."
                    + (f"\nJOIN only if price reaches {px(jl, coin)}: then {side(e_dir)} {qs(S['tQty'])} {nm}, stop {px(s0, coin)}"
                       if coin == "ETH" else ""))
            else:
                say(f"{'🟢' if e_dir == 1 else '🔴'} {nm} FILLED — you are {'LONG' if e_dir == 1 else 'SHORT'} {qs(S['tQty'])} {nm} "
                    f"at {px(e0, coin)} ({e_name}) @ {S['tLev']}x\n"
                    f"Place stop order: {side(-e_dir)} {qs(S['tQty'])} {nm} at {px(s0, coin)} (reduce-only)\n"
                    f"Size {S['sizeM']:.2f}x ({S['sWhy']})")

        # ---------- 2) manage the open trade (paper trades are managed the same way, silently)
        if S["pos"] != 0:
            pos = S["pos"]
            if (l <= S["stp"]) if pos == 1 else (h >= S["stp"]):
                xp = min(o, S["stp"]) if pos == 1 else max(o, S["stp"])
                tot = S["realized"] + S["rem"] * (xp - S["en"]) * pos + add_pl(xp)
                loss_stop = (S["stp"] - S["en"]) * pos < 0
                if S["paper"]:
                    say(f"⚪ {nm}: skipped trade ended {pts(tot)} pts — ready for the next signal.")
                else:
                    say(f"{'❌' if tot < 0 else '✅'} {nm} {'STOP HIT' if loss_stop else 'LOCKED STOP HIT'} {pts(tot)} pts — "
                        f"your stop order closed {qs(open_qty())} {nm}. Position is now 0.")
                S["hasLast"] = True
                S["lastRes"] = tot + S["rem"] * S["joinAdj"]
                S["lastPct"] = S["lastRes"] / S["en"] * 100
                S.update(joinPx=None, joinAdj=0.0, added=False, lastSide=pos, exitBar=i)
                S["reArm"] = (loss_stop and REON[coin] and S["reUsed"] < 1 and pos == S["sigSide"]
                              and t - S["sigT"] <= RE_HOURS * HOUR)
                if S["reArm"]:
                    rq = qty_for(S["R0"], size_for(S["sigSide"])[0])
                    say(f"🔵 {nm} RE-ENTRY READY: {side(S['sigSide'])} {qs(rq)} {nm} at market if a 5-min candle closes "
                        f"{'above' if S['sigSide'] == 1 else 'below'} {px(S['sigClose'], coin)} "
                        f"(only until {when(S['sigT'] + RE_HOURS * HOUR)}). I will message you when it happens.")
                S.update(pos=0, paper=False)
            else:
                tp1s = TP[coin][0] * S["kE"] * S["scE"]; tp2s = TP[coin][1] * S["kE"] * S["scE"]
                if TP[coin][0] > 0 and not S["g1"] and (h >= S["en"] + tp1s if pos == 1 else l <= S["en"] - tp1s):
                    S["realized"] += 0.5 * tp1s; S["rem"] -= 0.5; S["g1"] = True
                    cq = r3(min(S["held"], max(0.001, S["tQty"] * 0.5))); S["held"] = r3(S["held"] - cq)
                    if not S["paper"]:
                        say(f"💰 {nm} TP1 hit — CLOSE {qs(cq)} {nm} (50%) at +{tp1s:,.0f}. "
                            f"HOLD the other {qs(S['held'])} and change its stop order to {qs(S['held'])}.")
                if TP[coin][1] > 0 and S["g1"] and not S["g2"] and (h >= S["en"] + tp2s if pos == 1 else l <= S["en"] - tp2s):
                    S["realized"] += 0.3 * tp2s; S["rem"] -= 0.3; S["g2"] = True
                    cq = r3(min(S["held"], max(0.001, S["tQty"] * 0.3))); S["held"] = r3(S["held"] - cq)
                    if not S["paper"] and cq > 0:
                        say(f"💰 {nm} TP2 hit — CLOSE {qs(cq)} {nm} (30%) at +{tp2s:,.0f}. "
                            + (f"HOLD the last {qs(S['held'])} and change its stop order to {qs(S['held'])}." if S["held"] > 0
                               else "Position is now 0 — cancel its stop order."))
                fav = ((h - S["en"]) if pos == 1 else (S["en"] - l)) / S["R0"]
                if fav > S["best"]:
                    S["best"] = fav
                    if S["best"] >= BE[coin]:
                        lock = math.floor((S["best"] - BE[coin]) / STEP_R) * STEP_R
                        ns = S["en"] + pos * lock * S["R0"]
                        if (ns - S["stp"]) * pos > 0:
                            S["stp"] = ns
                            if not S["paper"]:
                                say(f"🔒 {nm} TRAIL: move stop to {'entry ' if lock == 0 else ''}{px(ns, coin)}"
                                    + ("" if lock == 0 else f" ({pts(lock * S['R0'])} locked)")
                                    + f" for all {qs(open_qty())} {nm} (reduce-only). Keep holding.")
                if not S["paper"] and coin == "ETH" and not S["added"] and S["best"] >= ADD_AT:
                    S["addPx"] = S["en"] + pos * ADD_AT * S["R0"]; S["added"] = True
                    aq = r3(max(0.001, S["tQty"] * ADD_Q)); S["held"] = r3(S["held"] + aq)
                    say(f"➕ {nm} ADD {qs(aq)} {nm} ({side(pos)}) at {px(S['addPx'], coin)} — same stop {px(S['stp'], coin)}. "
                        f"Now holding {qs(open_qty())} — change the stop order to {qs(open_qty())}.")
                if S["paper"] and coin == "ETH" and S["best"] >= JOIN_R:
                    jl = S["en"] + pos * JOIN_R * S["R0"]
                    S["joinPx"] = max(jl, o) if pos == 1 else min(jl, o)
                    S["joinAdj"] = (S["joinPx"] - S["en"]) * pos
                    S["realized"] -= S["rem"] * S["joinAdj"]; S["paper"] = False
                    say(f"{'🟢' if pos == 1 else '🔴'} {nm} JOIN — {side(pos)} {qs(S['tQty'])} {nm} at {px(S['joinPx'], coin)}\n"
                        f"Place stop order: {side(-pos)} {qs(S['tQty'])} {nm} at {px(S['stp'], coin)} (reduce-only)")

        # ---------- 3) new signal: same side = hold · opposite side = exit and reverse
        if sig_dir != 0:
            re_was = S["reArm"]
            S["reArm"] = False
            old_order = None
            if S["limDir"] != 0:
                if not will_skip(S["limDir"]):
                    old_order = f"{side(S['limDir'])} {qs(S['oQty'])} {nm} at {px(S['limPx'], coin)}"
                S["limDir"] = 0
            if S["pos"] != 0 and sig_dir == S["pos"]:
                if S["paper"]:
                    say(f"⚪ {nm} {sig_name} — skipped trade still running on paper. Nothing to do.")
                else:
                    say(f"{'🟢' if sig_dir == 1 else '🔴'} {nm} {sig_name} — you are already {'LONG' if sig_dir == 1 else 'SHORT'} "
                        f"{qs(open_qty())} {nm}: HOLD. Stop stays {px(S['stp'], coin)}. No new order.")
            if S["pos"] != 0 and sig_dir == -S["pos"]:
                pos = S["pos"]
                tot = S["realized"] + S["rem"] * (c - S["en"]) * pos + add_pl(c)
                if S["paper"]:
                    say(f"⚪ {nm}: skipped trade ended {pts(tot)} pts.")
                else:
                    say(f"{'✅' if tot >= 0 else '❌'} {nm} EXIT NOW — opposite signal ({sig_name}): CLOSE {qs(open_qty())} {nm} "
                        f"at market ({pts(tot)} pts) and cancel its stop order.")
                S["hasLast"] = True
                S["lastRes"] = tot + S["rem"] * S["joinAdj"]
                S["lastPct"] = S["lastRes"] / S["en"] * 100
                S.update(joinPx=None, joinAdj=0.0, added=False, lastSide=pos, pos=0, paper=False)
            if S["pos"] == 0:
                S.update(sigClose=c, sigT=t, sigSide=sig_dir, reUsed=0, pendRef=(l if sig_dir == 1 else h), pendRe=False,
                         pendName=sig_name, pendTyp=sig_typ)
                sk = will_skip(sig_dir)
                S["oSize"], S["oWhy"] = size_for(sig_dir)
                slpO = sl_for(sig_typ)
                eO = c * (1 + sig_dir * OFF / 100)
                S["oDist"] = eO * slpO / 100 if slpO > 0 else abs(eO - (l if sig_dir == 1 else h))
                S["oQty"] = qty_for(S["oDist"], S["oSize"]); S["oLev"] = lev_for(S["oDist"] / eO * 100)
                S.update(limDir=sig_dir, limTyp=sig_typ, limPx=eO, limRef=S["pendRef"], limName=sig_name,
                         limExp=t + LIM_WAIT * 60_000)
                pre = (f"First CANCEL the earlier {old_order} order.\n" if old_order else "") + ("The earlier RE-ENTRY idea is cancelled.\n" if re_was else "")
                if sk:
                    say(f"⚪ {nm} {sig_name} — SKIP ({skip_why()}). Do NOT place an order." + ("\n" + pre.strip() if pre else ""))
                else:
                    say(f"{'🟢' if sig_dir == 1 else '🔴'} {nm} {sig_name} — {pre}place a STOP order:\n"
                        f"{side(sig_dir)} {qs(S['oQty'])} {nm} if price goes {'above' if sig_dir == 1 else 'below'} {px(eO, coin)}\n"
                        f"Stop loss {px(eO - sig_dir * S['oDist'], coin)} · leverage {S['oLev']}x · "
                        f"cancel at {hhmm(S['limExp'])} IST if not filled\n"
                        f"Size {S['oSize']:.2f}x ({S['oWhy']})")
            elif old_order:
                say(f"⚪ {nm}: CANCEL the earlier {old_order} order.")
            elif re_was:
                say(f"⚪ {nm}: the re-entry idea is cancelled ({sig_name} signal).")
        # ---------- 4) re-entry: once, when a candle closes back past the signal candle's close
        elif S["reArm"] and S["pos"] == 0 and i > S["exitBar"]:
            if t - S["sigT"] > RE_HOURS * HOUR:
                S["reArm"] = False
            elif (S["sigSide"] == 1 and c > S["sigClose"]) or (S["sigSide"] == -1 and c < S["sigClose"]):
                d = S["sigSide"]
                S.update(pend=d, pendRe=True, pendName=("RE-ENTRY BUY" if d == 1 else "RE-ENTRY SELL"))
                S["oSize"], S["oWhy"] = size_for(d)
                S["oDist"] = S["R0"]; S["oQty"] = qty_for(S["R0"], S["oSize"]); S["oLev"] = lev_for(S["R0"] / c * 100)
                S["reUsed"] += 1; S["reArm"] = False
                say(f"{'🟢' if d == 1 else '🔴'} {nm} {S['pendName']} — {side(d)} {qs(S['oQty'])} {nm} at market NOW (≈{px(c, coin)}) @ {S['oLev']}x\n"
                    f"Stop loss ≈ {px(c - d * S['R0'], coin)} ({px(S['R0'], coin)} pts away) — exact stop follows after the fill.")

        # ---------- UNGER long (only while Markov's latest signal is BUY)
        if S["uPos"] == 1:
            u_stop = l <= S["uSt"]; u_time = t >= S["uExit"]; u_sell = sig_dir == -1
            if u_stop or u_time or u_sell:
                uxp = min(o, S["uSt"]) if u_stop else (o if u_time else c)
                say(f"🟠 {nm} UNGER EXIT ({'stop hit' if u_stop else 'time' if u_time else 'Markov SELL'}) {pts(uxp - S['uEn'])} pts — "
                    + ("your Unger stop order closed" if u_stop else "SELL")
                    + f" {qs(S['uQty'])} {nm} (Unger part only)" + ("." if u_stop else " at market and cancel the Unger stop order."))
                S["uPos"] = 0
        if sig_dir != 0:
            S["mkDir"] = sig_dir
        if (S["uPos"] == 0 and ung_win and S["uDone"] != s_key and S["mkDir"] == 1 and not fed_day and ung_ok
                and ung_lvl is not None and h >= ung_lvl):
            S["uEn"] = max(ung_lvl, o); S["uSt"] = S["uEn"] * (1 - UNG_STOP * volK / 100)
            ex = int(datetime(nyt.year, nyt.month, nyt.day, UNG_EXIT_H, 0, tzinfo=NY).timestamp() * 1000)
            S["uExit"] = ex + (DAY if UNG_EXIT_H <= UNG_FROM else 0)
            S["uPos"] = 1; S["uDone"] = s_key
            S["uQty"] = qty_for(S["uEn"] * UNG_STOP * volK / 100, UNG_MULT)
            say(f"🟠 {nm} UNGER BUY {qs(S['uQty'])} {nm} at {px(S['uEn'], coin)} @ {lev_for(UNG_STOP * volK)}x (extra, on top of any Markov trade)\n"
                f"Place stop order: SELL {qs(S['uQty'])} {nm} at {px(S['uSt'], coin)} (reduce-only)\n"
                f"Exit at {when(S['uExit'])}, or earlier on a Markov SELL")
    return ev, S


def status(coin: str, S: dict) -> str:
    nm = NAME[coin]
    if S["pos"] != 0 and S["paper"]:
        s = f"skipping (paper {'LONG' if S['pos'] == 1 else 'SHORT'}) — nothing to hold"
    elif S["pos"] != 0:
        s = (f"{'LONG' if S['pos'] == 1 else 'SHORT'} {qs(S['held'])} {nm} "
             f"from {px(S['en'], coin)}, stop {px(S['stp'], coin)}")
    elif S["limDir"] != 0:
        s = f"waiting order {'BUY' if S['limDir'] == 1 else 'SELL'} {qs(S['oQty'])} {nm} at {px(S['limPx'], coin)} until {hhmm(S['limExp'])} IST"
    elif S["reArm"]:
        s = f"re-entry ready if a candle closes {'above' if S['sigSide'] == 1 else 'below'} {px(S['sigClose'], coin)}"
    else:
        s = "no trade"
    if S["uPos"] == 1:
        s += f" + UNGER {qs(S['uQty'])} {nm}, stop {px(S['uSt'], coin)}"
    return s


# ═════════════════════════════════════════════════════════════════ Telegram + loop
def send(text: str, dry: bool) -> None:
    if dry:
        print("----\n" + text)
        return
    tok, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", data={"chat_id": chat, "text": text}, timeout=20)
    if not r.ok:
        print("telegram error", r.status_code, r.text[:200])


class Feed:
    """keeps the candles in memory; the replay always starts at the same UTC midnight 30 days back"""

    def __init__(self):
        self.start = (int(time.time() * 1000) // DAY - HISTORY_DAYS) * DAY
        self.rows = {}
        self.daily = {}

    def update(self):
        for name, src, sym, coin in WATCH:
            try:
                have = self.rows.get(coin)
                since = self.start if not have else max(have) - 2 * HOUR
                new = klines(src, sym, "5m", since)
                self.rows[coin] = {**(have or {}), **new}
                self.daily[coin] = daily_context(sorted(klines(src, sym, "1d", self.start - 1100 * DAY).values()))
            except Exception as e:
                print("price error", name, repr(e))
        try:
            self.daily["BTC_D"] = daily_context(sorted(klines(*BTC_DAILY, "1d", self.start - 1100 * DAY).values()))
        except Exception as e:
            print("price error BTC daily", repr(e))

    def run(self, coin):
        return run_coin(coin, sorted(self.rows[coin].values()), self.daily.get(coin, {}), self.daily.get("BTC_D", {}))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=58)
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    feed = Feed()
    if a.test:
        feed.update()
        r, dd = risk_pct()
        out = [f"✅ R&V v7 bot connected\nBalance ₹{cfg('ACCOUNT_BALANCE'):,.0f} · {dd * 100:.1f}% below high ₹{cfg('HIGHEST_BALANCE'):,.0f} · risk {r:.3f}% per trade"]
        for name, src, sym, coin in WATCH:
            if coin not in feed.rows:
                out.append(f"\n{name}: could not load prices"); continue
            rows = sorted(feed.rows[coin].values())
            ev, S = feed.run(coin)
            out.append(f"\n{name}: {px(rows[-1][4], coin)} (candle {hhmm(rows[-1][0] + TF)} IST) · volatility x{S['volK']:.2f}\nNow: {status(coin, S)}")
        send("\n".join(out), a.dry)
        print("\n".join(out))
        return
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    since = int(time.time() * 1000) - CATCH_UP_MIN * 60_000      # first run ever: only the last 30 minutes
    stop_at = time.time() + a.minutes * 60
    while True:
        feed.update()
        for name, src, sym, coin in WATCH:
            if coin not in feed.rows:
                continue
            try:
                ev, S = feed.run(coin)
                key = f"v7_{coin}"
                for close_ms, text in ev:
                    if close_ms > max(state.get(key, since), int(time.time() * 1000) - 3 * HOUR):
                        late = (time.time() * 1000 - close_ms) / 60000
                        send(text + f"\n🕒 candle closed {when(close_ms)}" + (f" (sent {late:.0f} min late)" if late > 3 else ""), a.dry)
                        print("sent", coin, when(close_ms))
                state[key] = max(state.get(key, 0), max(feed.rows[coin]) + TF)
            except Exception as e:                      # one bad call must not stop the bot
                print("error", name, repr(e))
        STATE.write_text(json.dumps(state))
        now = time.time()
        nxt = (now // 300 + 1) * 300 + 15                # 15 s after the next 5-minute candle closes
        if nxt > stop_at:
            break
        time.sleep(nxt - now)


if __name__ == "__main__":
    main()
