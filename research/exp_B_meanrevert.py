"""[B] 평균회귀 (횡보장) — BACKLOG.md 의 B 항목.

가설: 이 시장의 35%는 횡보(ADX<20)다. flip 계열 추세추종은 C·A2 에서 연달아 기각됐고
교훈은 '트리거 자체가 틀렸다'였다. 횡보 구간에선 역추세(RSI 과매도 매수·과매수 매도)가
맞는 트리거인가?

역추세 정석대로 청산한다: **지표 평균복귀**(롱 RSI>55 / 숏 RSI<45) + 시간청산 + ATR 손절.
트레일링은 역추세에 부적합해 안 쓴다(되돌림을 먹는 전략인데 되돌림에 나가면 앞뒤가 안 맞음).

롱/숏을 따로 돌린다 — 청산 조건이 방향별로 다르고(exit.condition 은 하나뿐), 매칭 귀무도
방향별이어야 공정하기 때문. 숏은 BTC 우상향 드리프트를 거스르므로 귀무 문턱이 낮아지는
대신 전략도 드리프트에 역행한다.

판정: 연도별로 돌려 **복수 연도에서 일관되게** 귀무 p95 를 넘어야 채택(한 해만이면 커브핏).

돌리기:  python3 -m research.exp_B_meanrevert [연도수]
"""
from __future__ import annotations

import datetime as dt
import sys

from engine.candles import TIMEFRAME_MINUTES
from research import lib
from research.exp_C_taker_delta import _median_hold_bars

SYMBOL = "BTCUSDT"
TIMEFRAME = "15m"          # A2 와 같은 잣대로 비교되게 통일
LEVERAGE = 1
EQUITY_PCT = 10            # null_model 의 size_fraction 과 일치(공정 비교)
YEARS = [2020, 2021, 2022, 2023, 2024, 2025]

_RSI = {"indicator": "RSI", "period": 14}
_ADX = {"indicator": "ADX", "period": 14}


def _ms(y: int, m: int = 1, d: int = 1) -> int:
    return int(dt.datetime(y, m, d, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _preset(side: str, adx_gate: bool) -> dict:
    """역추세 프리셋. side='long'|'short', adx_gate=True 면 횡보(ADX<20) 에서만 진입."""
    if side == "long":
        entry = [{"left": _RSI, "cmp": "<", "right": 30}]
        exit_cond = {"left": _RSI, "cmp": ">", "right": 55}      # 평균복귀 청산
    else:
        entry = [{"left": _RSI, "cmp": ">", "right": 70}]
        exit_cond = {"left": _RSI, "cmp": "<", "right": 45}
    if adx_gate:
        entry.append({"left": _ADX, "cmp": "<", "right": 20})
    children = entry
    return {
        "schemaVersion": "1.0",
        "name": f"meanrevert-{side}{'+adx' if adx_gate else ''}",
        "market": {"exchange": "binance-futures", "symbol": SYMBOL,
                   "timeframe": TIMEFRAME, "direction": side},
        "entryRules": [{"side": side, "when": {"op": "AND", "children": children}}],
        "entry": {"op": "AND", "children": children},
        "exit": {"stopLoss": {"type": "atrMultiple", "value": 2.0},
                 "condition": exit_cond,
                 "timeStop": {"maxBars": 30}},
        "sizing": {"leverage": LEVERAGE, "marginMode": "isolated",
                   "size": {"type": "equityPercent", "value": EQUITY_PCT},
                   "minLiquidationBuffer": 10, "maxConcurrentPositions": 1},
        "filter": {"cooldownBars": 2, "avoidFundingWindowMinutes": 5},
    }


VARIANTS = [
    ("① 롱 RSI<30 (게이트X)",      "long",  False),
    ("② 롱 RSI<30 + ADX<20",      "long",  True),
    ("③ 숏 RSI>70 (게이트X)",      "short", False),
    ("④ 숏 RSI>70 + ADX<20",      "short", True),
]


def main():
    years = YEARS[:int(sys.argv[1])] if len(sys.argv) > 1 else YEARS
    tf_min = TIMEFRAME_MINUTES[TIMEFRAME]
    tally = {}

    for y in years:
        base, fsched = lib.load(SYMBOL, start_ms=_ms(y), end_ms=_ms(y + 1))
        bh = (base.close[-1] / base.close[0] - 1) * 100
        print(f"\n{'='*104}\n[{y}] {SYMBOL} {TIMEFRAME} · {len(base):,}봉 · buy&hold {bh:+.1f}%")
        print(f"{'변형':<26} {'수익률':>8}  {'트레이드':>6}  {'승률':>6}  {'PF':>7}  {'MDD':>7}  {'수수료':>8}")
        print("-" * 104)

        for label, side, gate in VARIANTS:
            m = lib.backtest(base, _preset(side, gate), SYMBOL, funding_schedule=fsched)
            lib.show(label, m)
            if m.num_trades < 5:
                print("    ↳ 트레이드 부족(<5), 귀무 판정 생략")
                tally.setdefault(label, []).append(None)
                continue
            hold = _median_hold_bars(m, tf_min)
            nd = lib.null_model(base, TIMEFRAME, n_trades=m.num_trades, hold_bars=hold,
                                side=side, leverage=LEVERAGE, size_fraction=EQUITY_PCT / 100.0)
            v = lib.verdict(m.total_return_pct, nd)
            print(f"    ↳ 귀무: 전략 {v['strategy%']:+.2f}%  vs  랜덤{side} p95 {v['null_p95%']:+.2f}%"
                  f" (중앙값 {v['null_median%']:+.2f}%)  →  {v['verdict']}")
            tally.setdefault(label, []).append(v["beats_null"])

    print(f"\n{'='*104}\n[일관성] 복수 연도에서 '일관되게' 넘어야 엣지 (한 해만이면 커브핏)")
    for label, res in tally.items():
        hit = sum(1 for r in res if r)
        total = sum(1 for r in res if r is not None)
        if not total:
            mark = "판정 불가"
        elif hit == total:
            mark = "✅ 채택 후보"
        elif hit:
            mark = "⚠️ 일부만(커브핏 의심)"
        else:
            mark = "❌ 엣지 아님"
        print(f"  {label:<26} {hit}/{total} 연도 통과   {mark}")
    print("\n결과를 BACKLOG.md 의 B '결과' 칸에 적을 것.\n")


if __name__ == "__main__":
    main()
