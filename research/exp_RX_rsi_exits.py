"""RX — RSI 반대매매 + 역추세 정석 청산(RSI 50 복귀 익절 · 시간 청산 · 넓은 ATR 손절).

요청(2026-10-14): RO(고정 보유) 기각 뒤 "익절 기준은 뭐였어?" → 추천 청산 조합 "일단 돌려봐".
청산은 **하나로 사전 고정**한다(여러 개 돌려 고르면 그게 또 다중검정이다):
  · 익절   RSI 가 50 으로 돌아온 봉 종가(롱 RSI ≥ 50 · 숏 RSI ≤ 50)
  · 시간   24봉 안에 복귀 못 하면 24봉째 종가(5m 2시간 · 15m 6시간 · 1h 1일 · 4h 4일)
  · 손절   진입가 ∓ 2.5 × ATR(14, 신호봉) — 봉 고저로 검사, 갭이면 시가(불리한 쪽). 같은 봉에서 손절이 먼저.
진입은 RO 와 같다: RSI(p) 가 θ 아래로 들어가는 봉 롱 / 100−θ 위로 들어가는 봉 숏, 신호봉 종가. 포지션 하나.
격자: TF 5m·15m·1h·4h × p 7·14 × θ 20·25·30 × 방향 양쪽·롱만·숏만 = 72.
귀무(정밀): 거래마다 **실제 보유 길이·방향 그대로, 진입 시각만 무작위**, 종가→종가(engine/null_model.simulate_trades 와 같은 생각).
  롱숏이 섞이면 방향 귀무도 — 같은 진입 시각·같은 보유, 방향만 무작위.
판정(evt.search 와 같은 사전 규칙): 구간1·2 모두 수익 > 0 · 거래 ≥ 30 · 거래당 gross ≥ 12bp · 두 귀무 best-of-72 p95 초과
  → min(구간1,2) 최대 하나 → 홀드아웃 1회. gross 먼저, net(taker 왕복 10bp) 따로.

    python3 -u -m research.exp_RX_rsi_exits
"""
from __future__ import annotations

import itertools
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import indicators as ind   # noqa: E402
from engine import null_model as nm    # noqa: E402
from research import evt               # noqa: E402
from research.exp_RO_rsi_only import PERIODS, SIDES, TFS, THETAS, signal  # noqa: E402

TIME_STOP = 24
ATR_K = 2.5
MIN_BP = 12.0
GRID = list(itertools.product(TFS, PERIODS, THETAS, SIDES))
SAMPLES = 2000


class Res:
    def __init__(self, idx, side, hold, raw, why, fee):
        self.idx, self.side, self.hold, self.raw, self.why = idx, side, hold, raw, why
        self.n = len(idx)
        pt = np.clip(1.0 + side * raw - 2 * fee, 0.0, None)
        self.ret = float((pt.prod() - 1) * 100) if self.n else 0.0
        self.mean_bp = float((side * raw).mean() * 1e4) if self.n else 0.0
        self.win = float(((side * raw) > 2 * fee).mean() * 100) if self.n else 0.0


_ATR: dict = {}


def trades(b, combo, fee):
    tf, p, th, side_mode = combo
    rsi = ind.rsi(b.close, p)
    k = evt.pin(b)
    if k not in _ATR:
        _ATR[k] = ind.atr(b.high, b.low, b.close, 14)
    atr = _ATR[k]
    si, ss = signal(b, (tf, p, th, side_mode, 0))
    o = np.argsort(si, kind="stable")
    si, ss = si[o], ss[o]
    n = len(b.close)
    idx, sd, hold, raw, why = [], [], [], [], []
    free = -1
    for i, s in zip(si, ss):
        if i < free or i + TIME_STOP >= n or not np.isfinite(atr[i]):
            continue
        e = b.close[i]
        stop = e - s * ATR_K * atr[i]
        for j in range(i + 1, i + TIME_STOP + 1):
            if s > 0 and b.low[j] <= stop:
                x, w = min(b.open[j], stop), "손절"
                break
            if s < 0 and b.high[j] >= stop:
                x, w = max(b.open[j], stop), "손절"
                break
            if (s > 0 and rsi[j] >= 50) or (s < 0 and rsi[j] <= 50):
                x, w = b.close[j], "익절"
                break
        else:
            x, w = b.close[i + TIME_STOP], "시간"
        idx.append(i); sd.append(s); hold.append(j - i); raw.append(x / e - 1); why.append(w)
        free = j
    return Res(np.array(idx, int), np.array(sd, float), np.array(hold, int), np.array(raw), why, fee)


def nulls(b, r, fee, seed=0):
    c = b.close
    n = len(c)
    rng = np.random.default_rng(seed)
    t = np.ones(SAMPLES)
    for h, s in zip(r.hold, r.side):
        i = rng.integers(0, n - h - 1, size=SAMPLES)
        t *= np.clip(1.0 + s * (c[i + h] / c[i] - 1.0) - 2 * fee, 0.0, None)
    time_null = (t - 1) * 100
    if (r.side > 0).all() or (r.side < 0).all():
        return time_null, None
    raw_cc = c[r.idx + r.hold] / c[r.idx] - 1.0
    sides = rng.permuted(np.tile(r.side, (SAMPLES, 1)), axis=1)
    side_null = (np.clip(1.0 + sides * raw_cc[None, :] - 2 * fee, 0.0, None).prod(axis=1) - 1) * 100
    return time_null, side_null


def judge(b, r, fee, n_combos):
    tn, sn = nulls(b, r, fee)
    g = nm.gate(r.ret, tn, n_combos=n_combos)
    key = "bestOfNP95" if n_combos > 1 else "nullP95"
    sp = None if sn is None else nm.gate(r.ret, sn, n_combos=n_combos)[key]
    ok = (r.ret > g[key] and (sp is None or r.ret > sp) and r.ret > 0 and r.n >= evt.MIN_TRADES
          and r.mean_bp >= MIN_BP)
    return g[key], sp, ok, g


def label(c):
    tf, p, th, s = c
    return f"{tf:>3} RSI{p:<2} {th}/{100 - th} {s:5}"


def main():
    segs = evt.segments()
    print(f"RX · RSI 반대매매 + RSI50 익절 · {TIME_STOP}봉 시간청산 · {ATR_K}×ATR 손절 · {evt.SYMBOL} · 격자 {len(GRID)}")
    for i, (a, b) in enumerate(segs):
        print(f"  구간{i+1}: {evt._ym(a)} ~ {evt._ym(b)}{'  (홀드아웃)' if i == 2 else ''}")
    for stage, fee in (("gross(수수료 0)", 0.0), ("net(taker 왕복 10bp)", evt.TAKER)):
        print(f"\n── {stage} " + "─" * 50, flush=True)
        res = {(c, i): trades(evt.bars(c[0], i), c, fee) for c in GRID for i in (0, 1)}
        for tf in TFS:
            sub = [c for c in GRID if c[0] == tf]
            r0 = [res[(c, 0)].ret for c in sub]
            r1 = [res[(c, 1)].ret for c in sub]
            allw = [w for c in sub for i in (0, 1) for w in res[(c, i)].why]
            mix = " · ".join(f"{w} {allw.count(w) / max(1, len(allw)) * 100:.0f}%" for w in ("익절", "시간", "손절"))
            win = np.median([res[(c, i)].win for c in sub for i in (0, 1)])
            bp = max(min(res[(c, 0)].mean_bp, res[(c, 1)].mean_bp) for c in sub)
            print(f"  {tf:>3}: 중앙 {np.median(r0):+7.1f}% / {np.median(r1):+7.1f}%  두 구간 양수 "
                  f"{sum(a > 0 and b > 0 for a, b in zip(r0, r1))}/{len(sub)}  거래당 최고 {bp:+5.1f}bp  "
                  f"승률 중앙 {win:.0f}%  청산 {mix}")
        cand = [c for c in GRID if all(res[(c, i)].ret > 0 and res[(c, i)].n >= evt.MIN_TRADES
                                       and res[(c, i)].mean_bp >= MIN_BP for i in (0, 1))]
        cand.sort(key=lambda c: -min(res[(c, 0)].ret, res[(c, 1)].ret))
        print(f"  후보(두 구간 양수·거래 ≥{evt.MIN_TRADES}·거래당 ≥{MIN_BP}bp): {len(cand)}/{len(GRID)}")
        passed = []
        for c in cand:
            js = [judge(evt.bars(c[0], i), res[(c, i)], fee, len(GRID)) for i in (0, 1)]
            ok = all(j[2] for j in js)
            if ok:
                passed.append(c)
            a, b_ = res[(c, 0)], res[(c, 1)]
            side = " / ".join("-" if j[1] is None else f"{j[1]:+.0f}" for j in js)
            print(f"   {label(c):28} {a.ret:+7.1f}% {a.n:4d} {a.mean_bp:+6.1f}bp | {b_.ret:+7.1f}% {b_.n:4d} "
                  f"{b_.mean_bp:+6.1f}bp | 시간귀무 p95 {js[0][0]:+.0f} / {js[1][0]:+.0f} · 방향 {side}  {'✅' if ok else '❌'}")
        if not passed:
            print(f"  ★ {stage}: 사전 규칙을 통과한 조합 없음 → 홀드아웃을 열지 않는다.")
            continue
        pk = passed[0]
        b3 = evt.bars(pk[0], 2)
        r3 = trades(b3, pk, fee)
        p95, sp, ok, g = judge(b3, r3, fee, 1)
        print(f"  ★ 선택 {label(pk)} → 홀드아웃 {r3.ret:+.1f}% · 거래 {r3.n} · {r3.mean_bp:+.1f}bp · "
              f"귀무 p95 {p95:+.1f}% · 백분위 {g['percentile']:.0f} → {'✅ 통과' if ok else '❌'}")


if __name__ == "__main__":
    main()
