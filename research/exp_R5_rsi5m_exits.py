"""R5 — 5분봉 RSI 역추세 진입 × 어울리는 청산 조합 탐색.

요청(2026-10-14): "5분 봉과 rsi 역추세 진입과 + 어울리는 청산 전략을 추천해서 조합을 잘 맞춰봐".
배경: RO 에서 5m RSI 역추세는 gross 로 되돌림이 실재했다(고정 보유 중앙 +45/+12%) — 그러나 거래당 ≤4.9bp 로
왕복 taker 10bp 를 못 넘었다. RX(청산 하나 고정)는 손절이 익절을 먹어 오히려 나빠졌다. 이번엔 청산을 격자로 연다.

진입(신호봉 종가):
  · enter   RSI(p) 가 θ 아래로 **들어가는** 봉 롱 / 100−θ 위로 들어가는 봉 숏 (RO 와 같다)
  · confirm 구역에 있다가 θ 위로 **빠져나오는** 봉 롱 / 100−θ 아래로 빠져나오는 봉 숏 (반등 확인 후)
  p 7·14 × θ 15·20·25·30 × 방향 양쪽·롱만·숏만 × 모드 2 = 48
청산(먼저 닿는 것, 같은 봉이면 손절 → 익절 순 = 불리한 쪽):
  · 익절 rsi50  RSI 가 50 으로 돌아온 봉 종가     · rsi60  롱 RSI ≥ 60 / 숏 ≤ 40 (평균을 넘어설 때까지)
         atr1 / atr2  진입가 ± 1·2 × ATR(14) 지정가(봉 고저로 검사, 갭이면 시가)   · none  시간 청산만
  · 손절 none · 1.5 × ATR · 3 × ATR (봉 고저, 갭이면 시가)
  · 시간 12봉(1시간) · 48봉(4시간) 종가
  5 × 3 × 2 = 30  →  격자 48 × 30 = 1,440
판정(결과 전 고정, evt 와 같은 규칙):
  ① 후보 = 구간1·2 모두 수익 > 0 · 거래 ≥ 30 · 거래당 gross ≥ 12bp(경제 문턱 — 왕복 taker 10bp + 여유)
     · 정밀 시간 귀무(거래별 보유·방향 그대로, 시각만 무작위) best-of-1440 p95 초과 · 롱숏이면 방향 귀무도
  ② 후보 중 min(구간1, 구간2) 최대 하나 → ③ 홀드아웃(구간3) 1회 — 수익 > 0 · 귀무 p95 초과 · 거래당 ≥ 12bp
  gross(수수료 0) 먼저, net(taker 5bp × 2) 따로. 익절 지정가도 taker 로 친다(보수적).

    python3 -u -m research.exp_R5_rsi5m_exits
"""
from __future__ import annotations

import itertools
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import indicators as ind   # noqa: E402
from engine import null_model as nm    # noqa: E402
from research import evt               # noqa: E402

TF = "5m"
PERIODS = (7, 14)
THETAS = (15, 20, 25, 30)
SIDES = ("both", "long", "short")
MODES = ("enter", "confirm")
TPS = ("rsi50", "rsi60", "atr1", "atr2", "none")
SLS = (None, 1.5, 3.0)
TIMES = (12, 48)
ENTRIES = list(itertools.product(PERIODS, THETAS, SIDES, MODES))
EXITS = list(itertools.product(TPS, SLS, TIMES))
GRID = [(e, x) for e in ENTRIES for x in EXITS]
MIN_BP = 12.0
SAMPLES = 2000

_CACHE: dict = {}


def feats(b, p):
    k = (evt.pin(b), p)
    if k not in _CACHE:
        _CACHE[k] = (ind.rsi(b.close, p), ind.atr(b.high, b.low, b.close, 14))
    return _CACHE[k]


def entries(b, e):
    p, th, side, mode = e
    r, _ = feats(b, p)
    prev = np.concatenate(([np.nan], r[:-1]))
    with np.errstate(invalid="ignore"):
        if mode == "enter":
            lo = (r < th) & (prev >= th)
            hi = (r > 100 - th) & (prev <= 100 - th)
        else:
            lo = (r >= th) & (prev < th)
            hi = (r <= 100 - th) & (prev > 100 - th)
    i_lo = np.nonzero(lo)[0] if side in ("both", "long") else np.array([], int)
    i_hi = np.nonzero(hi)[0] if side in ("both", "short") else np.array([], int)
    idx = np.concatenate((i_lo, i_hi))
    sd = np.concatenate((np.ones(len(i_lo)), -np.ones(len(i_hi))))
    o = np.argsort(idx, kind="stable")
    return idx[o], sd[o]


class Res:
    __slots__ = ("idx", "side", "hold", "raw", "why", "n")

    def __init__(self, idx, side, hold, raw, why):
        self.idx, self.side, self.hold, self.raw, self.why = idx, side, hold, raw, why
        self.n = len(idx)

    def ret(self, fee):
        if not self.n:
            return 0.0
        return float((np.clip(1.0 + self.side * self.raw - 2 * fee, 0.0, None).prod() - 1) * 100)

    def bp(self):
        return float((self.side * self.raw).mean() * 1e4) if self.n else 0.0


_TR: dict = {}


def trades(seg, e, x):
    key = (seg, e, x)
    if key in _TR:
        return _TR[key]
    b = evt.bars(TF, seg)
    tp, sl, tmax = x
    r, atr = feats(b, e[0])
    O, H, L, C = b.open.tolist(), b.high.tolist(), b.low.tolist(), b.close.tolist()
    R, A = r.tolist(), atr.tolist()
    n = len(C)
    si, ss = entries(b, e)
    idx, sd, hold, raw, why = [], [], [], [], []
    free = -1
    for i, s in zip(si.tolist(), ss.tolist()):
        if i < free or i + tmax >= n or not (A[i] == A[i]):
            continue
        en = C[i]
        stop = None if sl is None else en - s * sl * A[i]
        tgt = None
        if tp == "atr1":
            tgt = en + s * A[i]
        elif tp == "atr2":
            tgt = en + s * 2 * A[i]
        rlim = 50.0 if tp == "rsi50" else (60.0 if s > 0 else 40.0) if tp == "rsi60" else None
        px, w, j = None, "시간", i + tmax
        for j in range(i + 1, i + tmax + 1):
            if stop is not None:
                if s > 0 and L[j] <= stop:
                    px, w = min(O[j], stop), "손절"; break
                if s < 0 and H[j] >= stop:
                    px, w = max(O[j], stop), "손절"; break
            if tgt is not None:
                if s > 0 and H[j] >= tgt:
                    px, w = max(O[j], tgt), "익절"; break
                if s < 0 and L[j] <= tgt:
                    px, w = min(O[j], tgt), "익절"; break
            if rlim is not None and ((s > 0 and R[j] >= rlim) or (s < 0 and R[j] <= rlim)):
                px, w = C[j], "익절"; break
        if px is None:
            px, j = C[i + tmax], i + tmax
        idx.append(i); sd.append(s); hold.append(j - i); raw.append(px / en - 1.0); why.append(w)
        free = j
    res = Res(np.array(idx, int), np.array(sd, float), np.array(hold, int), np.array(raw), why)
    _TR[key] = res
    return res


def nulls(seg, t, fee, seed=0):
    c = evt.bars(TF, seg).close
    n = len(c)
    rng = np.random.default_rng(seed)
    tot = np.ones(SAMPLES)
    for h, s in zip(t.hold, t.side):
        i = rng.integers(0, n - h - 1, size=SAMPLES)
        tot *= np.clip(1.0 + s * (c[i + h] / c[i] - 1.0) - 2 * fee, 0.0, None)
    tn = (tot - 1) * 100
    if (t.side > 0).all() or (t.side < 0).all():
        return tn, None
    # 방향 귀무: 같은 진입 시각·같은 보유, 방향만 무작위(종가→종가)
    cc = c[t.idx + t.hold] / c[t.idx] - 1.0
    sides = rng.permuted(np.tile(t.side, (SAMPLES, 1)), axis=1)
    sn = (np.clip(1.0 + sides * cc[None, :] - 2 * fee, 0.0, None).prod(axis=1) - 1) * 100
    return tn, sn


def judge(seg, t, fee, n_combos):
    tn, sn = nulls(seg, t, fee)
    k = "bestOfNP95" if n_combos > 1 else "nullP95"
    g = nm.gate(t.ret(fee), tn, n_combos=n_combos)
    sp = None if sn is None else nm.gate(t.ret(fee), sn, n_combos=n_combos)[k]
    ok = (t.ret(fee) > max(g[k], 0) and (sp is None or t.ret(fee) > sp)
          and t.n >= evt.MIN_TRADES and t.bp() >= MIN_BP)
    return g[k], sp, ok, g["percentile"]


def label(c):
    (p, th, side, mode), (tp, sl, tm) = c
    return f"RSI{p:<2} {th}/{100 - th} {side:5} {mode:7} | 익절 {tp:5} 손절 {'-' if sl is None else f'{sl}A':4} {tm}봉"


def main():
    segs = evt.segments()
    print(f"R5 · 5m RSI 역추세 × 청산 · {evt.SYMBOL} · 진입 {len(ENTRIES)} × 청산 {len(EXITS)} = {len(GRID)}")
    for i, (a, b) in enumerate(segs):
        print(f"  구간{i+1}: {evt._ym(a)} ~ {evt._ym(b)}{'  (홀드아웃)' if i == 2 else ''}")
    for i in (0, 1):
        for c in GRID:
            trades(i, *c)
        print(f"  구간{i+1} 거래 계산 완료", flush=True)

    # 청산 계열별 요약 — 무엇이 거래당 bp 를 움직이나
    print(f"\n  청산별(진입 48 위 중앙) · gross 거래당 bp 구간1/2 · 승률 · 청산 사유")
    for x in EXITS:
        bps = [[trades(i, e, x).bp() for e in ENTRIES] for i in (0, 1)]
        wins = [np.mean((trades(i, e, x).side * trades(i, e, x).raw) > 0) * 100
                for i in (0, 1) for e in ENTRIES if trades(i, e, x).n]
        ws = [w for i in (0, 1) for e in ENTRIES for w in trades(i, e, x).why]
        mix = " ".join(f"{w}{ws.count(w) / max(1, len(ws)) * 100:3.0f}%" for w in ("익절", "손절", "시간"))
        tp, sl, tm = x
        print(f"   익절 {tp:5} 손절 {'-' if sl is None else f'{sl}A':4} {tm:2}봉 | "
              f"{np.median(bps[0]):+5.1f} / {np.median(bps[1]):+5.1f}bp  최고 {max(min(a, b) for a, b in zip(*bps)):+5.1f}  "
              f"승률 {np.median(wins):3.0f}%  {mix}")

    for stage, fee in (("gross(수수료 0)", 0.0), ("net(taker 왕복 10bp)", evt.TAKER)):
        print(f"\n── {stage} " + "─" * 50, flush=True)
        both = [c for c in GRID if all(trades(i, *c).ret(fee) > 0 and trades(i, *c).n >= evt.MIN_TRADES for i in (0, 1))]
        cand = [c for c in both if min(trades(0, *c).bp(), trades(1, *c).bp()) >= MIN_BP]
        cand.sort(key=lambda c: -min(trades(0, *c).ret(fee), trades(1, *c).ret(fee)))
        print(f"  두 구간 양수 {len(both)}/{len(GRID)} · 그중 거래당 ≥{MIN_BP}bp {len(cand)}")
        show = cand[:15] if cand else sorted(both, key=lambda c: -min(trades(0, *c).bp(), trades(1, *c).bp()))[:8]
        passed = []
        for c in cand:
            js = [judge(i, trades(i, *c), fee, len(GRID)) for i in (0, 1)]
            if all(j[2] for j in js):
                passed.append(c)
            if c in show or all(j[2] for j in js):
                a, b_ = trades(0, *c), trades(1, *c)
                sp = " / ".join("-" if j[1] is None else f"{j[1]:+.0f}" for j in js)
                print(f"   {label(c):62} {a.ret(fee):+7.1f}% {a.n:5d} {a.bp():+5.1f}bp | {b_.ret(fee):+7.1f}% {b_.n:5d} "
                      f"{b_.bp():+5.1f}bp | 귀무 p95 {js[0][0]:+.0f}/{js[1][0]:+.0f} 방향 {sp} {'✅' if all(j[2] for j in js) else '❌'}")
        if not cand:
            print("   (문턱 미달 — 두 구간 양수 중 거래당 bp 상위)")
            for c in show:
                a, b_ = trades(0, *c), trades(1, *c)
                print(f"   {label(c):62} {a.ret(fee):+7.1f}% {a.n:5d} {a.bp():+5.1f}bp | "
                      f"{b_.ret(fee):+7.1f}% {b_.n:5d} {b_.bp():+5.1f}bp")
        if not passed:
            print(f"  ★ {stage}: 사전 규칙 ① 통과 없음 → 홀드아웃을 열지 않는다.")
            continue
        pk = passed[0]
        t3 = trades(2, *pk)
        p95, sp, ok, pct = judge(2, t3, fee, 1)
        print(f"  ★ 선택 {label(pk)} → 홀드아웃 {t3.ret(fee):+.1f}% · 거래 {t3.n} · {t3.bp():+.1f}bp · "
              f"귀무 p95 {p95:+.1f}% · 백분위 {pct:.0f} → {'✅ 통과' if ok else '❌'}")


if __name__ == "__main__":
    main()
