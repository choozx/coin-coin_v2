"""DV5 — 5분봉 RSI 다이버전스 진입의 파라미터 탐색 (DV 후속).

요청(2026-10-14): "5분봉에서 더 최적의 값들을 찾을 수 있어?"
DV(54조합)에서 5m 은 거래당 gross 중앙 −0.9/+1.9bp 였다. 격자를 넓히면 다중검정이 커지므로 **검증을 더 세게** 건다:
BTC 구간1·2 에서 고르고 → BTC 구간3 + **한 번도 안 본 ETH·SOL 5m 전 기간(2021-01~)** 에 숫자 그대로.

진입·청산 정의는 DV 와 같다(손절 = 저점2, 같은 봉이면 손절 먼저). 열어 둔 값:
  RSI 기간 7·14·21 × n 20·25·30·35 × (저점 최소 간격, 창) (3,30)·(5,60)·(10,120) × 다이버전스 강도 R2−R1 > 0·≥5
  × 방향 롱·숏·양쪽 × 익절 RSI60(숏 40)·1R·2R·3R × 시간 24·96봉 = 1,728
판정(결과 전 고정):
  ① 후보 = BTC 구간1·2 모두 net(taker 왕복 10bp) 수익 > 0 · 거래 ≥ 30 · 거래당 gross ≥ 12bp
     · 정밀 시간 귀무(거래별 보유·방향, 시각만 무작위, 같은 수수료) best-of-1728 p95 초과 · 롱숏이면 방향 귀무도
  ② 후보 중 min(구간1, 구간2) net 최대 하나
  ③ 통과 = BTC 구간3 net > 0 · 단일 귀무 p95 초과  그리고  ETH·SOL 전 기간 net 둘 다 > 0
  후보가 없으면 '두 구간 net 양수' 상위를 참고로만 찍고 홀드아웃·ETH·SOL 은 열지 않는다.

    python3 -u -m research.exp_DV5_opt
"""
from __future__ import annotations

import itertools
import sys
import time

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs  # noqa: E402
from engine import indicators as ind   # noqa: E402
from engine import null_model as nm    # noqa: E402
from engine.candles import resample    # noqa: E402
from research import evt, lib          # noqa: E402

PS = (7, 14, 21)
NS = (20, 25, 30, 35)
GW = ((3, 30), (5, 60), (10, 120))
DELTAS = (0.0, 5.0)
SIDES = ("long", "short", "both")
TPS = ("rsi", "1R", "2R", "3R")
TIMES = (24, 96)
ENTRY = list(itertools.product(PS, NS, GW, DELTAS, SIDES))
EXIT = list(itertools.product(TPS, TIMES))
GRID = [(e, x) for e in ENTRY for x in EXIT]
FEE = evt.TAKER
MIN_BP = 12.0
SAMPLES = 2000


def sig_long(L, R, n, gap, win, delta):
    out = []
    t1 = None
    for t in range(1, len(L)):
        r = R[t]
        if r != r:
            continue
        if t1 is not None and t - t1 > win:
            t1 = None
        if t1 is None:
            if r < n:
                t1, P1, R1, P2, tP2 = t, L[t], r, None, None
            continue
        ref = P1 if P2 is None else P2
        if L[t] < ref:
            if t - t1 >= gap and r > R1 + delta:
                P2, tP2 = L[t], t
            elif t - t1 < gap:
                P1, R1, t1, P2 = L[t], min(R1, r), t, None
            else:
                P1, R1, t1, P2 = L[t], r, t, None
            continue
        if r < R1:
            R1, t1, P2 = r, t, None
            continue
        if P2 is not None and t > tP2 and r >= n and r > R[t - 1]:
            out.append((t, P2))
            t1 = None
    return out


class Data:
    def __init__(self, b):
        self.b = b
        self.O, self.H, self.L, self.C = (x.tolist() for x in (b.open, b.high, b.low, b.close))
        self.nL = (-b.high).tolist()
        self.R = {p: ind.rsi(b.close, p).tolist() for p in PS}
        self.Rm = {p: [100.0 - x if x == x else x for x in r] for p, r in self.R.items()}
        self._e = {}

    def entries(self, e):
        p, n, (gap, win), d, side = e
        key = (p, n, gap, win, d)
        if key not in self._e:
            lo = [(t, 1.0, s) for t, s in sig_long(self.L, self.R[p], n, gap, win, d)]
            hi = [(t, -1.0, -s) for t, s in sig_long(self.nL, self.Rm[p], n, gap, win, d)]
            self._e[key] = (lo, hi)
        lo, hi = self._e[key]
        return sorted((lo if side != "short" else []) + (hi if side != "long" else []))

    def trades(self, e, x):
        tp, tmax = x
        O, H, L, C, R = self.O, self.H, self.L, self.C, self.R[e[0]]
        N = len(C)
        idx, sd, hold, raw = [], [], [], []
        free = -1
        for i, s, stop in self.entries(e):
            if i < free or i + tmax >= N:
                continue
            en = C[i]
            risk = (en - stop) * s
            if risk <= 0:
                continue
            tgt = en + s * {"1R": 1, "2R": 2, "3R": 3}[tp] * risk if tp != "rsi" else None
            px = None
            for j in range(i + 1, i + tmax + 1):
                if (s > 0 and L[j] <= stop) or (s < 0 and H[j] >= stop):
                    px = min(O[j], stop) if s > 0 else max(O[j], stop); break
                if tgt is not None and ((s > 0 and H[j] >= tgt) or (s < 0 and L[j] <= tgt)):
                    px = max(O[j], tgt) if s > 0 else min(O[j], tgt); break
                if tgt is None and ((s > 0 and R[j] >= 60) or (s < 0 and R[j] <= 40)):
                    px = C[j]; break
            if px is None:
                j, px = i + tmax, C[i + tmax]
            idx.append(i); sd.append(s); hold.append(j - i); raw.append(px / en - 1)
            free = j
        return Tr(np.array(idx, int), np.array(sd), np.array(hold, int), np.array(raw))


class Tr:
    def __init__(self, idx, side, hold, raw):
        self.idx, self.side, self.hold, self.raw, self.n = idx, side, hold, raw, len(idx)

    def ret(self, fee=FEE):
        return float((np.clip(1 + self.side * self.raw - 2 * fee, 0, None).prod() - 1) * 100) if self.n else 0.0

    def bp(self):
        return float((self.side * self.raw).mean() * 1e4) if self.n else 0.0


def judge(c, t, n_combos, fee=FEE):
    rng = np.random.default_rng(0)
    tot = np.ones(SAMPLES)
    for h, s in zip(t.hold, t.side):
        i = rng.integers(0, len(c) - h - 1, size=SAMPLES)
        tot *= np.clip(1 + s * (c[i + h] / c[i] - 1) - 2 * fee, 0, None)
    k = "bestOfNP95" if n_combos > 1 else "nullP95"
    g = nm.gate(t.ret(fee), (tot - 1) * 100, n_combos=n_combos)
    sp = None
    if not ((t.side > 0).all() or (t.side < 0).all()):
        cc = c[t.idx + t.hold] / c[t.idx] - 1
        sides = rng.permuted(np.tile(t.side, (SAMPLES, 1)), axis=1)
        sp = nm.gate(t.ret(fee), (np.clip(1 + sides * cc - 2 * fee, 0, None).prod(1) - 1) * 100, n_combos=n_combos)[k]
    ok = t.ret(fee) > max(g[k], 0) and (sp is None or t.ret(fee) > sp)
    return g[k], sp, ok, g["percentile"]


def label(c):
    (p, n, (g, w), d, side), (tp, tm) = c
    return f"RSI{p:<2} n={n} 간격{g}/{w} Δ{d:.0f} {side:5} 익절 {tp:3} {tm}봉"


def main():
    t0 = time.time()
    segs = evt.segments()
    print(f"DV5 · 5m RSI 다이버전스 탐색 · 격자 {len(GRID)} · 고르기 BTC 구간1·2 → 확인 BTC 구간3 + ETH·SOL")
    D = [Data(evt.bars("5m", i)) for i in (0, 1)]
    res = {}
    for k, c in enumerate(GRID):
        res[c] = (D[0].trades(*c), D[1].trades(*c))
        if k % 500 == 0:
            print(f"  {k}/{len(GRID)} · {time.time() - t0:.0f}초", flush=True)
    bp_all = [min(a.bp(), b.bp()) for a, b in res.values() if min(a.n, b.n) >= evt.MIN_TRADES]
    print(f"\n  거래당 gross bp(두 구간 중 낮은 쪽) 분포: 중앙 {np.median(bp_all):+.1f} · 상위 1% {np.percentile(bp_all, 99):+.1f} · "
          f"최고 {max(bp_all):+.1f} · ≥12bp {sum(x >= MIN_BP for x in bp_all)}개")
    for side in SIDES:
        v = [min(a.bp(), b.bp()) for c, (a, b) in res.items() if c[0][4] == side and min(a.n, b.n) >= evt.MIN_TRADES]
        print(f"    {side:5}: 중앙 {np.median(v):+.1f} · 최고 {max(v):+.1f}bp")
    both = [c for c, (a, b) in res.items() if a.ret() > 0 and b.ret() > 0 and min(a.n, b.n) >= evt.MIN_TRADES]
    cand = [c for c in both if min(res[c][0].bp(), res[c][1].bp()) >= MIN_BP]
    print(f"\n  net 두 구간 양수 {len(both)}/{len(GRID)} · 그중 거래당 ≥{MIN_BP}bp {len(cand)}")
    cand.sort(key=lambda c: -min(res[c][0].ret(), res[c][1].ret()))
    passed = []
    for c in cand:
        a, b = res[c]
        js = [judge(D[i].b.close, t, len(GRID)) for i, t in ((0, a), (1, b))]
        ok = all(j[2] for j in js)
        if ok:
            passed.append(c)
        sp = " / ".join("-" if j[1] is None else f"{j[1]:+.0f}" for j in js)
        print(f"   {label(c):52} {a.ret():+6.1f}% {a.n:4d}건 {a.bp():+5.1f}bp | {b.ret():+6.1f}% {b.n:4d}건 {b.bp():+5.1f}bp"
              f" | 귀무 p95 {js[0][0]:+.0f}/{js[1][0]:+.0f} 방향 {sp} {'✅' if ok else '❌'}", flush=True)
    if not cand:
        print("   (문턱 미달 — net 두 구간 양수 상위, 참고)")
        for c in sorted(both, key=lambda c: -min(res[c][0].ret(), res[c][1].ret()))[:8]:
            a, b = res[c]
            print(f"   {label(c):52} {a.ret():+6.1f}% {a.n:4d}건 {a.bp():+5.1f}bp | {b.ret():+6.1f}% {b.n:4d}건 {b.bp():+5.1f}bp")
    if not passed:
        print("\n  ★ 사전 규칙 ① 통과 없음 → BTC 구간3·ETH·SOL 은 열지 않는다.")
        print(f"  ({time.time() - t0:.0f}초)")
        return
    pk = passed[0]
    d3 = Data(evt.bars("5m", 2))
    t3 = d3.trades(*pk)
    p95, sp, ok3, pct = judge(d3.b.close, t3, 1)
    ok3 = ok3 and t3.ret() > 0
    print(f"\n  ★ 선택 {label(pk)}")
    print(f"    BTC 구간3: net {t3.ret():+.1f}% · {t3.n}건 · {t3.bp():+.1f}bp · 귀무 p95 {p95:+.1f}% · 백분위 {pct:.0f} {'✅' if ok3 else '❌'}")
    oks = [ok3]
    for s in ("ETHUSDT", "SOLUSDT"):
        st = cs.stats(s)
        bb = resample(lib.load(s, start_ms=st["min"], end_ms=st["max"], with_funding=False)[0], 5)
        d = Data(bb)
        t = d.trades(*pk)
        p95, _, _, pct = judge(bb.close, t, 1)
        oks.append(t.ret() > 0)
        print(f"    {s}: net {t.ret():+.1f}% · gross {t.ret(0):+.1f}% · {t.n}건 · {t.bp():+.1f}bp · 귀무 p95 {p95:+.1f}% · 백분위 {pct:.0f} "
              f"{'✅' if t.ret() > 0 else '❌'}")
    print(f"  판정: {'✅ 통과' if all(oks) else '❌'}  ({time.time() - t0:.0f}초)")


if __name__ == "__main__":
    main()
