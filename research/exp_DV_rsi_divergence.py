"""DV — RSI 과매도 + 상승 다이버전스 + RSI 복귀 확인 진입 (숏은 대칭).

요청(2026-10-14, 사용자 설계): 롱 기준
  1. RSI 가 n 보다 낮아졌었고   2. 상승 다이버전스가 나타났고   3. 봉 마감 때 RSI 가 n 보다 높아졌을 때 진입.
R5 의 confirm 진입이 사실상 1+3 이었다(기각). **새로 더해지는 건 2(다이버전스)** — 그게 bp 를 올리는지가 질문이다.

정의(롱, 봉 종가 기준·인과적 — 결과 전 고정, 사용자가 추천값 승인):
  · 저점1: RSI < n 인 봉에서 시작. P1 = 그 뒤 가장 낮은 저가, R1 = 가장 낮은 RSI. 60봉 안에 진입 못 하면 버린다.
  · 저점2(다이버전스): 저점1 뒤 5봉 이상 지나 저가가 P1(또는 이전 P2)보다 낮아지고 그 봉 RSI > R1 → P2·R2 기록.
    더 낮은 저가에서 RSI 도 R1 이하면 다이버전스가 아니라 새 저점1 로 갱신. 5봉 안의 더 낮은 저가는 같은 저점(P1 갱신).
    가격은 안 낮아졌는데 RSI 가 R1 아래로 가면 R1 갱신·P2 무효.
  · 진입: P2 봉 뒤, 저가 > P2(새 저점 아님) · RSI ≥ n · RSI 상승 마감인 첫 봉 종가.
    (R2 < n 이면 'n 위로 복귀하는 봉', R2 ≥ n 이면 '저점2 뒤 처음 상승 마감한 봉' — 사용자 질문 1 의 추천 해석)
청산(먼저 닿는 것, 같은 봉이면 손절 먼저):
  · 손절 = P2(다이버전스가 틀린 자리) — 봉 저가로 검사, 갭이면 시가.
  · 익절 A = RSI ≥ 60 봉 종가 · 익절 B = 진입가 + 1.5 × (진입가 − P2) 지정가.
  · 시간 = 48봉 종가.
격자: TF 5m·15m·1h × n 25·30·35 × 방향 롱·숏·양쪽 × 익절 2 = 54.
판정(R5 와 같은 규칙): 구간1·2 모두 수익 > 0 · 거래 ≥ 30 · 거래당 gross ≥ 12bp · 정밀 시간 귀무 best-of-54 p95 초과
  (롱숏이면 방향 귀무도) → min(구간1, 2) 최대 하나 → 홀드아웃 1회. gross 먼저, net(taker 왕복 10bp) 따로.
대조(판정 밖): 같은 기계에서 다이버전스 조건(R2 > R1)만 뺀 진입 — 다이버전스가 거래당 bp 를 올리는가.

    python3 -u -m research.exp_DV_rsi_divergence
"""
from __future__ import annotations

import itertools
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import indicators as ind   # noqa: E402
from engine import null_model as nm    # noqa: E402
from research import evt               # noqa: E402

TFS = ("5m", "15m", "1h")
NS = (25, 30, 35)
SIDES = ("long", "short", "both")
TPS = ("rsi60", "r1.5")
GRID = list(itertools.product(TFS, NS, SIDES, TPS))
MIN_GAP, WINDOW, TMAX, RSI_P = 5, 60, 48, 14
MIN_BP = 12.0
SAMPLES = 2000


def signals_long(L, R, n, div=True):
    """롱 진입 (봉 index, 손절가 P2) 목록. L 저가, R RSI(list)."""
    out = []
    t1 = None
    for t in range(1, len(L)):
        r = R[t]
        if r != r:
            continue
        if t1 is not None and t - t1 > WINDOW:
            t1 = None
        if t1 is None:
            if r < n:
                t1, P1, R1, P2, R2, tP2 = t, L[t], r, None, None, None
            continue
        ref = P1 if P2 is None else P2
        if L[t] < ref:
            if t - t1 >= MIN_GAP and (r > R1 or not div):
                P2, R2, tP2 = L[t], r, t
            elif t - t1 < MIN_GAP:
                P1, R1, t1 = L[t], min(R1, r), t
                P2 = None
            else:                                   # 더 낮은 저가 + RSI 도 더 낮음 → 새 저점1
                P1, R1, t1, P2 = L[t], r, t, None
            continue
        if r < R1:
            R1, t1, P2 = r, t, None
            continue
        if P2 is not None and t > tP2 and r >= n and r > R[t - 1]:
            out.append((t, P2))
            t1 = None
    return out


def entries(b, n, side, div=True):
    k = (evt.pin(b), "rsi")
    if k not in _C:
        _C[k] = ind.rsi(b.close, RSI_P).tolist()
    R = _C[k]
    res = []
    if side in ("long", "both"):
        res += [(t, 1.0, p) for t, p in signals_long(b.low.tolist(), R, n, div)]
    if side in ("short", "both"):          # 거울: 가격·RSI 를 뒤집어 같은 함수로
        Rm = [100.0 - x if x == x else x for x in R]
        res += [(t, -1.0, -p) for t, p in signals_long((-b.high).tolist(), Rm, n, div)]
    return sorted(res)


_C: dict = {}


class Res:
    def __init__(self, idx, side, hold, raw, why):
        self.idx, self.side, self.hold, self.raw, self.why = (np.array(idx, int), np.array(side, float),
                                                              np.array(hold, int), np.array(raw), why)
        self.n = len(idx)

    def ret(self, fee):
        return float((np.clip(1 + self.side * self.raw - 2 * fee, 0, None).prod() - 1) * 100) if self.n else 0.0

    def bp(self):
        return float((self.side * self.raw).mean() * 1e4) if self.n else 0.0


_T: dict = {}


def trades(tf, seg, n, side, tp, div=True):
    key = (tf, seg, n, side, tp, div)
    if key in _T:
        return _T[key]
    b = evt.bars(tf, seg)
    O, H, L, C = b.open.tolist(), b.high.tolist(), b.low.tolist(), b.close.tolist()
    R = _C.get((evt.pin(b), "rsi")) or ind.rsi(b.close, RSI_P).tolist()
    N = len(C)
    idx, sd, hold, raw, why = [], [], [], [], []
    free = -1
    for i, s, stop in entries(b, n, side, div):
        if i < free or i + TMAX >= N:
            continue
        en = C[i]
        if (en - stop) * s <= 0:
            continue
        tgt = en + s * 1.5 * abs(en - stop) if tp == "r1.5" else None
        px, w = None, "시간"
        for j in range(i + 1, i + TMAX + 1):
            if (s > 0 and L[j] <= stop) or (s < 0 and H[j] >= stop):
                px, w = (min(O[j], stop) if s > 0 else max(O[j], stop)), "손절"; break
            if tgt is not None and ((s > 0 and H[j] >= tgt) or (s < 0 and L[j] <= tgt)):
                px, w = (max(O[j], tgt) if s > 0 else min(O[j], tgt)), "익절"; break
            if tp == "rsi60" and ((s > 0 and R[j] >= 60) or (s < 0 and R[j] <= 40)):
                px, w = C[j], "익절"; break
        if px is None:
            j, px = i + TMAX, C[i + TMAX]
        idx.append(i); sd.append(s); hold.append(j - i); raw.append(px / en - 1); why.append(w)
        free = j
    _T[key] = Res(idx, sd, hold, raw, why)
    return _T[key]


def judge(tf, seg, t, fee, n_combos):
    c = evt.bars(tf, seg).close
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
    ok = t.ret(fee) > max(g[k], 0) and (sp is None or t.ret(fee) > sp) and t.n >= evt.MIN_TRADES and t.bp() >= MIN_BP
    return g[k], sp, ok, g["percentile"]


def label(c):
    tf, n, side, tp = c
    return f"{tf:>3} n={n} {side:5} 익절 {tp}"


def main():
    segs = evt.segments()
    print(f"DV · RSI({RSI_P}) 과매도 + 다이버전스 + 복귀 확인 · 손절 P2 · {TMAX}봉 · {evt.SYMBOL} · 격자 {len(GRID)}")
    for i, (a, b) in enumerate(segs):
        print(f"  구간{i+1}: {evt._ym(a)} ~ {evt._ym(b)}{'  (홀드아웃)' if i == 2 else ''}")
    print("\n  TF별 · gross 거래당 bp 중앙(구간1/2) · 거래 수 중앙 · 승률 · 청산 | 대조: 다이버전스 뺀 같은 진입")
    for tf in TFS:
        sub = [c for c in GRID if c[0] == tf]
        T = [trades(c[0], i, *c[1:]) for c in sub for i in (0, 1)]
        Tc = [trades(c[0], i, *c[1:], div=False) for c in sub for i in (0, 1)]
        ws = [w for t in T for w in t.why]
        mix = " ".join(f"{w}{ws.count(w) / max(1, len(ws)) * 100:3.0f}%" for w in ("익절", "손절", "시간"))
        b0 = np.median([trades(c[0], 0, *c[1:]).bp() for c in sub])
        b1 = np.median([trades(c[0], 1, *c[1:]).bp() for c in sub])
        c0 = np.median([trades(c[0], 0, *c[1:], div=False).bp() for c in sub])
        c1 = np.median([trades(c[0], 1, *c[1:], div=False).bp() for c in sub])
        win = np.median([np.mean(t.side * t.raw > 0) * 100 for t in T if t.n])
        print(f"   {tf:>3}: {b0:+5.1f} / {b1:+5.1f}bp · 거래 {np.median([t.n for t in T]):5.0f} · 승률 {win:3.0f}% · {mix}"
              f" | 대조 {c0:+5.1f} / {c1:+5.1f}bp · 거래 {np.median([t.n for t in Tc]):5.0f}", flush=True)
    for stage, fee in (("gross(수수료 0)", 0.0), ("net(taker 왕복 10bp)", evt.TAKER)):
        print(f"\n── {stage} " + "─" * 50)
        rows = []
        for c in GRID:
            a, b_ = trades(c[0], 0, *c[1:]), trades(c[0], 1, *c[1:])
            rows.append((c, a, b_))
        both = [r for r in rows if r[1].ret(fee) > 0 and r[2].ret(fee) > 0 and min(r[1].n, r[2].n) >= evt.MIN_TRADES]
        cand = [r for r in both if min(r[1].bp(), r[2].bp()) >= MIN_BP]
        print(f"  두 구간 양수 {len(both)}/{len(GRID)} · 그중 거래당 ≥{MIN_BP}bp {len(cand)}")
        cand.sort(key=lambda r: -min(r[1].ret(fee), r[2].ret(fee)))
        passed = []
        show = cand or sorted(both, key=lambda r: -min(r[1].bp(), r[2].bp()))[:6]
        for c, a, b_ in show:
            js = [judge(c[0], i, t, fee, len(GRID)) for i, t in ((0, a), (1, b_))] if (c, a, b_) in cand else None
            ok = js is not None and all(j[2] for j in js)
            if ok:
                passed.append(c)
            tail = (f"귀무 p95 {js[0][0]:+.0f}/{js[1][0]:+.0f} 방향 "
                    + " / ".join("-" if j[1] is None else f"{j[1]:+.0f}" for j in js) + f" {'✅' if ok else '❌'}") if js else "(문턱 미달)"
            print(f"   {label(c):26} {a.ret(fee):+7.1f}% {a.n:4d}건 {a.bp():+6.1f}bp | {b_.ret(fee):+7.1f}% {b_.n:4d}건 "
                  f"{b_.bp():+6.1f}bp | {tail}")
        if not passed:
            print(f"  ★ {stage}: 사전 규칙 ① 통과 없음 → 홀드아웃을 열지 않는다.")
            continue
        pk = passed[0]
        t3 = trades(pk[0], 2, *pk[1:])
        p95, sp, ok, pct = judge(pk[0], 2, t3, fee, 1)
        print(f"  ★ 선택 {label(pk)} → 홀드아웃 {t3.ret(fee):+.1f}% · {t3.n}건 · {t3.bp():+.1f}bp · 귀무 p95 {p95:+.1f}% · "
              f"백분위 {pct:.0f} → {'✅ 통과' if ok else '❌'}")


if __name__ == "__main__":
    main()
