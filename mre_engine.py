"""Python copy of pine/markov_regime_v6_flip.pine (default settings) — same BUY / SELL / FLIP / CONT rules.

Feed candles oldest-first (only CLOSED candles). step() returns the label printed on that candle, or "".
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timezone


class MRE:
    def __init__(self, symbol: str, lookback=20, threshold=0.75, metal_threshold=0.4, matrix_window=200,
                 min_diverge=0.10, use_m2=True, edge=0.20, z_len=50, z_thresh=0.5, atr_len=14,
                 rank_len=200, rank_min=20.0, rank_max=95.0, weekend_mode="Auto", line_hours=24,
                 buf_pct=0.01, cancel_ref=True, bar_ms=300_000):
        s = symbol.upper()
        self.metal = any(k in s for k in ("XAU", "XAG", "GOLD", "SILVER"))
        self.th = metal_threshold if self.metal else threshold
        self.lb, self.win, self.mind, self.use_m2, self.edge = lookback, matrix_window, min_diverge, use_m2, edge
        self.zlen, self.zth, self.atrlen, self.rlen, self.rmin, self.rmax = z_len, z_thresh, atr_len, rank_len, rank_min, rank_max
        self.wmode, self.line_ms, self.buf, self.cancel = weekend_mode, line_hours * 3_600_000, buf_pct / 100.0, cancel_ref
        # standard (every candle) series
        self.closes = deque(maxlen=max(lookback, z_len) + 1)
        self.prev_c = None
        self.tr_seed: list[float] = []
        self.atr = None
        self.atr_hist = deque(maxlen=rank_len + 1)
        # weekday-only series
        self.wc = deque(maxlen=max(lookback, z_len) + 1)
        self.w_prev_c = None
        self.w_rma = None
        self.w_atr = deque(maxlen=rank_len + 1)
        self.w_ret = self.w_z = self.w_rk = None
        # weekend auto-detect
        self.wkd_sum = self.wkn_sum = 0.0
        self.wkd_n = self.wkn_n = 0
        # Markov state
        self.counts = [0] * 9
        self.ring: deque[int] = deque()
        self.prev_state = None
        self.sig = None
        self.last_sig = 0
        # flip state
        self.hi = self.lo = self.cand = None
        self.dir = 0
        self.flipped = False
        self.live_ms = 0
        self.bar_ms = bar_ms
        self.state_name = "–"

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _zs(vals, n, c):
        v = list(vals)[-n:]
        m = sum(v) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / n)
        return (c - m) / sd if sd > 0 else 0.0

    @staticmethod
    def _rank(hist, rlen):
        if len(hist) < rlen + 1:
            return None
        cur = hist[-1]
        return 100.0 * sum(1 for x in list(hist)[:-1] if x <= cur) / rlen

    @staticmethod
    def _is_weekend(t_ms):
        d = datetime.fromtimestamp(t_ms / 1000, timezone.utc)
        wd, h = d.weekday(), d.hour        # Mon=0 .. Sun=6
        return (wd == 4 and h >= 21) or wd == 5 or (wd == 6 and h < 22)

    # ---------------------------------------------------------------- main
    def step(self, t: int, o: float, h: float, l: float, c: float) -> str:
        wknd = self._is_weekend(t)
        if c > 0:
            if wknd:
                self.wkn_sum += (h - l) / c; self.wkn_n += 1
            else:
                self.wkd_sum += (h - l) / c; self.wkd_n += 1
        auto_dead = self.wkd_n >= 300 and self.wkn_n >= 300 and (self.wkn_sum / self.wkn_n) < 0.35 * (self.wkd_sum / self.wkd_n)
        skip = self.wmode == "Always ignore" or (self.wmode == "Auto" and auto_dead)
        use_bar = (not skip) or (not wknd)

        # standard series
        tr = h - l if self.prev_c is None else max(h, self.prev_c) - min(l, self.prev_c)
        self.prev_c = c
        if self.atr is None:
            self.tr_seed.append(tr)
            if len(self.tr_seed) == self.atrlen:
                self.atr = sum(self.tr_seed) / self.atrlen
        else:
            self.atr = (self.atr * (self.atrlen - 1) + tr) / self.atrlen
        if self.atr is not None:
            self.atr_hist.append(self.atr)
        self.closes.append(c)
        ret_std = (c - self.closes[-1 - self.lb]) / self.closes[-1 - self.lb] * 100 if len(self.closes) > self.lb else None
        z_std = self._zs(self.closes, self.zlen, c) if len(self.closes) >= self.zlen else None
        rk_std = self._rank(self.atr_hist, self.rlen)

        # weekday-only series
        if not wknd:
            wtr = h - l if self.w_prev_c is None else max(h, self.w_prev_c) - min(l, self.w_prev_c)
            self.w_rma = wtr if self.w_rma is None else (self.w_rma * (self.atrlen - 1) + wtr) / self.atrlen
            self.w_prev_c = c
            self.wc.append(c)
            n = len(self.wc)
            self.w_ret = (c - self.wc[-1 - self.lb]) / self.wc[-1 - self.lb] * 100 if n > self.lb else None
            if n >= self.zlen:
                self.w_z = self._zs(self.wc, self.zlen, c)
            self.w_atr.append(self.w_rma)
            self.w_rk = self._rank(self.w_atr, self.rlen)

        ret = self.w_ret if skip else ret_std
        z = (self.w_z or 0.0) if skip else z_std
        rk = self.w_rk if skip else rk_std
        st = None if ret is None else 0 if ret >= self.th else 2 if ret <= -self.th else 1
        vol_ok = rk is not None and self.rmin <= rk <= self.rmax
        zz = z if z is not None else 0.0
        stat = 1 if not vol_ok else 0 if zz >= self.zth else 2 if zz <= -self.zth else 1

        buy = sell = False
        if st is not None and use_bar:
            if self.prev_state is not None:
                code = self.prev_state * 3 + st
                self.ring.append(code); self.counts[code] += 1
                if len(self.ring) > self.win:
                    old = self.ring.popleft(); self.counts[old] -= 1
            self.prev_state = st
            self.state_name = ("BULL", "SIDEWAYS", "BEAR")[st]
            P = []
            for i in range(3):
                rs = self.counts[i * 3] + self.counts[i * 3 + 1] + self.counts[i * 3 + 2]
                P.append([self.counts[i * 3 + j] / rs if rs > 0 else 1 / 3 for j in range(3)])
            M2 = [[sum(P[i][k] * P[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
            p = [1 / 3, 1 / 3, 1 / 3]
            for _ in range(60):
                q = [sum(p[i] * P[i][j] for i in range(3)) for j in range(3)]
                tot = sum(q); p = [x / tot for x in q]
            row = P[st]
            prev_sig = self.sig
            self.sig = row[0] - row[2]
            sig2 = M2[st][0] - M2[st][2]
            dist = sum(abs(row[j] - p[j]) for j in range(3))
            informative = dist >= self.mind
            dual = (st == 0 and stat == 0) or (st == 2 and stat == 2)
            up = prev_sig is not None and self.sig > self.edge and prev_sig <= self.edge
            dn = prev_sig is not None and self.sig < -self.edge and prev_sig >= -self.edge
            if up and dual and st == 0 and informative and (not self.use_m2 or sig2 > 0) and self.last_sig != 1:
                self.last_sig = 1; buy = True
            if dn and dual and st == 2 and informative and (not self.use_m2 or sig2 < 0) and self.last_sig != -1:
                self.last_sig = -1; sell = True

        # lines expire after line_hours, counting only candles in use (gold: weekend hours don't count)
        if use_bar and self.hi is not None:
            self.live_ms += self.bar_ms
        if self.line_ms > 0 and self.hi is not None and self.live_ms >= self.line_ms:
            self.hi = self.lo = self.cand = None; self.flipped = False

        label = ""
        if use_bar and self.hi is not None and not (buy or sell):
            b = c * self.buf
            if self.dir == 1:
                if not self.flipped:
                    if self.cand is None:
                        if c < self.lo - b: self.cand = l
                    elif c < self.cand - b:
                        label = "FLIP SELL"; self.flipped = True; self.cand = None
                    elif self.cancel and c > self.hi + b:
                        self.cand = None
                elif c > self.hi + b:
                    label = "CONT BUY"; self.flipped = False
            elif self.dir == -1:
                if not self.flipped:
                    if self.cand is None:
                        if c > self.hi + b: self.cand = h
                    elif c > self.cand + b:
                        label = "FLIP BUY"; self.flipped = True; self.cand = None
                    elif self.cancel and c < self.lo - b:
                        self.cand = None
                elif c < self.lo - b:
                    label = "CONT SELL"; self.flipped = False

        if buy or sell:
            self.hi, self.lo, self.cand, self.flipped = h, l, None, False
            self.live_ms = 0
            self.dir = 1 if buy else -1
            label = "BUY" if buy else "SELL"
        return label
