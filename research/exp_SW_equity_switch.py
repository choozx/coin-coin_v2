"""SW — 전략 성적 스위치: 전략이 '잘 될 때만' 켜고 '안 될 때' 끈다(자기 최근 성적 기준).

요청(2026-10-07): "스위칭 신호가 좋을 때와 안 좋을 때를 판별해서 전략을 바꾸는 스위치가 필요하겠는데?"
시장 상태 스위치(ADX·변동성·이평)는 일부 봤다(A2 기각·E 악화·SRM/B3 의 1d SMA200 은 효과). 그런데 15m 조합이 반 토막 난
2021 은 ETH 강한 상승 추세 — 시장 상태로는 '켤' 구간이었다. 그래서 **전략 자신의 최근 성적**으로 켜고 끄는 쪽을 본다.

규칙(결과 전 고정): 대상 = SC 선택 조합(15m ST(14,2.5)+RSI<70+EMA200 롱만), 1배·자본 100%, 전부 maker 2bp(사용자 가정).
  모든 신호를 '가상 거래'로 기록(쉬는 동안에도) → i 번째 거래는 직전 N개 가상 거래 손익 합 > 0 일 때만 실제로 한다.
  N = 5 · 10 · 20. 처음 N 개는 켠 상태로 시작.
귀무: 같은 개수만큼 **무작위로 쉬기** 2,000회 — 거래를 줄여서 좋아진 건지, 쉴 때를 맞힌 건지 가른다.
통과: 한 N 이 BTC 세 구간 + ETH + SOL 다섯 시험 중 **≥4 개**에서 무작위 쉬기의 95 백분위를 넘는다.

    python3 -u -m research.exp_SW_equity_switch
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                        # noqa: E402
from research import lib                                     # noqa: E402
from research.exp_SC_scalp_combo import build                # noqa: E402
from research.exp_T_supertrend_search import _ym, segments   # noqa: E402

C = ("15m", (14, 2.5), "noext", "ema200", "long")
ONE = {"leverage": 1, "marginMode": "isolated", "size": {"type": "equityPercent", "value": 100.0}}
NS = (5, 10, 20)
FEE = 0.0002
SAMPLES = 2000


def trade_returns(sym, a, b):
    base, fs = lib.load(sym, start_ms=a, end_ms=b)
    p = build(*C, symbol=sym)
    p["sizing"] = ONE
    m = lib.backtest(base, p, sym, funding_schedule=fs, maker_fee=FEE, taker_fee=FEE)
    eq, out = 10000.0, []
    for t in m.trades:                          # 1배·100% 라 거래당 수익률 = pnl / 직전 자산
        out.append(t.pnl / eq)
        eq += t.pnl
    return np.array(out)


def switch(r, n):
    on = np.ones(len(r), bool)
    for i in range(n, len(r)):
        on[i] = r[i - n:i].sum() > 0
    return on


def total(r, on):
    return float((np.prod(1.0 + r * on) - 1.0) * 100.0)


def main() -> int:
    tests = [(f"BTC 구간{i+1}", "BTCUSDT", a, b) for i, (a, b) in enumerate(segments())]
    for sym in ("ETHUSDT", "SOLUSDT"):
        tests.append((sym[:3] + " 2021~", sym, 1609459200000, cs.stats(sym)["max"]))
    print(f"SW · 전략 성적 스위치 · {C} · 1배 · maker 2bp · 무작위 쉬기 {SAMPLES}회")
    rng = np.random.default_rng(0)
    passes = {n: 0 for n in NS}
    for name, sym, a, b in tests:
        r = trade_returns(sym, a, b)
        line = f"  {name:12} 거래 {len(r):4d} · 스위치 없음 {total(r, np.ones(len(r), bool)):+7.1f}%"
        for n in NS:
            on = switch(r, n)
            k = int(on.sum())
            null = np.array([total(r, np.isin(np.arange(len(r)), rng.choice(len(r), k, replace=False)))
                             for _ in range(SAMPLES)])
            s = total(r, on)
            pct = float((null < s).mean() * 100)
            ok = s > np.percentile(null, 95)
            passes[n] += ok
            line += f" | N{n}: {s:+7.1f}% (켠 비율 {k / len(r):.0%}, 백분위 {pct:3.0f}{' ✅' if ok else ''})"
        print(line, flush=True)
    print("\n  판정(다섯 중 ≥4 에서 무작위 쉬기 p95 초과): " + " · ".join(
        f"N{n} {passes[n]}/5 {'✅' if passes[n] >= 4 else '❌'}" for n in NS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
