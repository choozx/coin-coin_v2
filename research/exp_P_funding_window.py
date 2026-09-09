"""P — 펀딩 정산 전후에 체계적 왜곡이 있는가 (5~15분 스캘핑 후보).

가설: 펀딩은 8시간마다(00/08/16 UTC) 정산된다. **내야 하는 쪽**은 정산 직전 포지션을
줄이고(가격을 그쪽으로 밀고) 직후 되돌린다. 그렇다면 정산 시각 주변에 방향 예측이 아닌
**구조적 자금 흐름**이 남는다.

왜 이걸 고르나: 이 프로젝트의 기각 8건은 전부 '방향 예측'이었다. 그리고 5~15분에서
taker 왕복 0.10% 는 15분봉 평균 움직임(0.153%)의 65%, 중앙값(0.098%)의 100% 다 —
지표를 아무리 골라도 이 벽을 못 넘는다. 남는 길은 예측이 아닌 **구조**뿐이고, 펀딩은
이 시장에서 가장 확실한 구조적 현금흐름이다(H·I·J·M 이 전부 거기서 나왔다).

보유가 수 분~수십 분이라 진짜 스캘핑에 맞고, 하루 3번이라 표본도 충분하다.

방법:
  1) 정산 시각마다 전후 구간 수익률을 재고 **펀딩 부호별로** 나눠 본다(측정).
  2) 거기서 규칙을 만들어 실제 손익을 내고 **귀무 게이트**에 건다(판정).
     격자(진입 오프셋 × 보유)를 훑으므로 다중검정 보정이 반드시 필요하다 —
     engine/null_model.gate(n_combos=...) 가 그 일을 한다.
  3) **세 독립 구간**에서 다 통과해야 후보다. 한 구간의 통과는 거의 항상 잡음이었다.

    python3 -m research.exp_P_funding_window [DAYS]
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import binance_math as bm, candle_store, null_model as nm   # noqa: E402
from engine.candles import MINUTE_MS                                    # noqa: E402

SYMBOL = "BTCUSDT"
LEV, FRAC = 10, 0.10          # 라이브 프리셋과 같은 노출(레버리지 10 · 증거금 10%)
# 진입 오프셋(정산 기준 분) × 보유(분). 음수 = 정산 전.
OFFSETS = (-30, -15, -5, 0)
HOLDS = (5, 15, 30, 60)


def _price_at(ot, close, t_ms):
    """t_ms 이하의 마지막 1분봉 종가 인덱스. 없으면 None."""
    i = int(np.searchsorted(ot, t_ms, side="right")) - 1
    return i if 0 <= i < len(close) else None


def measure(ot, close, times, rates):
    """정산 전후 구간 수익률을 펀딩 부호별로. **먼저 보고, 그 다음에 규칙을 만든다.**"""
    print("\n  ── 정산 시각 주변 평균 수익률(bp) · 펀딩 부호별 ──")
    print(f"    {'구간':>12} {'펀딩>0':>10} {'펀딩<0':>10} {'전체':>10}")
    print("    " + "─" * 46)
    for a, b in ((-30, -15), (-15, 0), (0, 15), (15, 30), (0, 60)):
        pos, neg = [], []
        for t, r in zip(times, rates):
            i, j = _price_at(ot, close, t + a * MINUTE_MS), _price_at(ot, close, t + b * MINUTE_MS)
            if i is None or j is None or close[i] <= 0:
                continue
            ret = (close[j] / close[i] - 1) * 10_000
            (pos if r > 0 else neg).append(ret)
        allr = pos + neg
        if not allr:
            continue
        print(f"    {f'{a:+d}~{b:+d}분':>12} {np.mean(pos) if pos else 0:9.2f} "
              f"{np.mean(neg) if neg else 0:9.2f} {np.mean(allr):9.2f}")
    print("    (부호가 반대로 갈리면 '펀딩 내는 쪽이 민다'는 가설과 맞는다)")


def strategy_return(ot, close, times, rates, offset, hold, taker):
    """규칙: 펀딩>0 이면 숏, <0 이면 롱 — **내는 쪽 반대편에 선다.** 복리 총수익률(%)."""
    eq, n = 1.0, 0
    rt_fee = 2 * taker * LEV * FRAC
    for t, r in zip(times, rates):
        if r == 0:
            continue
        i = _price_at(ot, close, t + offset * MINUTE_MS)
        j = _price_at(ot, close, t + (offset + hold) * MINUTE_MS)
        if i is None or j is None or close[i] <= 0:
            continue
        side = -1.0 if r > 0 else 1.0
        ret = close[j] / close[i] - 1.0
        eq *= max(0.0, 1.0 + side * LEV * FRAC * ret - rt_fee)
        n += 1
    return (eq - 1.0) * 100.0, n


def run_segment(base, times, rates, taker, tag):
    ot, close = base.open_time, base.close.astype(np.float64)
    times = [t for t in times if ot[0] <= t <= ot[-1]]
    rates = [rates[t] for t in times]
    print(f"\n  {tag} · {len(base):,}봉 · 정산 {len(times)}회")
    if len(times) < 30:
        print("    표본 부족 — 건너뜀")
        return {}
    measure(ot, close, times, rates)
    combos = [(o, h) for o in OFFSETS for h in HOLDS]
    print(f"\n  ── 규칙 판정({len(combos)}조합, 귀무 게이트) ──")
    print(f"    {'진입':>6} {'보유':>5} {'수익률':>9} {'거래':>5} {'귀무p95':>9} "
          f"{'격자보정':>9} {'판정':>10}")
    print("    " + "─" * 60)
    out = {}
    for off, hold in combos:
        ret, n = strategy_return(ot, close, times, rates, off, hold, taker)
        if n < 30:
            continue
        dist = nm.simulate(base, 1, n, hold, "long", leverage=LEV, size_fraction=FRAC,
                           samples=1500, taker_fee=taker)
        g = nm.gate(ret, dist, n_combos=len(combos))
        v = "✅통과" if g["beatsBestOfN"] else ("△귀무만" if g["beatsNull"] else "❌미달")
        if g["negative"]:
            v += "(음수)"
        out[(off, hold)] = g["beatsBestOfN"] and not g["negative"]
        print(f"    {off:+6d} {hold:5d} {ret:+8.2f}% {n:5d} {g['nullP95']:9.2f} "
              f"{g['bestOfNP95']:9.2f} {v:>10}")
    return out


def main() -> int:
    taker = bm.fees_for_symbol(SYMBOL)[1]
    st = candle_store.stats(SYMBOL)
    lo, hi = st["min"], st["max"]
    span = (hi - lo) // 3
    print(f"P · 펀딩 정산 전후 · {SYMBOL} · 독립 3구간 · taker {taker*1e4:.0f}bp/편도")
    print(f"  노출: 레버리지 {LEV} · 증거금 {FRAC*100:.0f}% → 왕복 수수료 "
          f"{2*taker*LEV*FRAC*100:.2f}%(자본 대비)")
    passed = []
    for i in range(3):
        a, b = lo + i * span, lo + (i + 1) * span
        base = candle_store.load_range(SYMBOL, a, b)
        sched = candle_store.funding_schedule(SYMBOL, a, b)
        if not sched:
            print(f"\n  구간 {i+1}/3 · 펀딩 데이터 없음 — 건너뜀")
            continue
        passed.append(run_segment(base, sorted(sched), sched, taker, f"구간 {i+1}/3"))
    print("\n  ── 세 구간 공통 판정 ──")
    common = [k for k in (passed[0] if passed else {})
              if all(p.get(k) for p in passed)] if len(passed) == 3 else []
    print(f"    세 구간 모두 통과(음수 제외): {common or '없음'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
