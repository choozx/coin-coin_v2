"""[A2] 추세추종 + ADX, 레짐 구간 재검증 — BACKLOG.md 의 A2 항목.

가설: 나이브 추세추종(SuperTrend flip 10/3)은 전체기간 평균으론 엣지가 아니었지만,
추세추종은 원래 추세장에서만 성립한다. **상승추세 구간만** 떼고 **방향성 게이트**
(ADX>25 & +DI>-DI)를 걸면 엣지가 드러나는가?

C 에서 배운 것: 병목은 필터가 아니라 트리거(flip)다 — flip 은 상승장에서도 휩쏘를 잡는다.
A2 는 그 트리거를 '레짐 조건부'로 제한하면 살아나는지를 본다. 안 살아나면 flip 계열을
닫고 B(평균회귀)로 간다.

비교 3변형(전부 롱온리 — 상승 구간이므로):
  ① flip 만                    (기준선, 게이트 없음)
  ② flip + ADX>25              (edge-research 가설 A 그대로)
  ③ flip + ADX>25 & +DI>-DI    (A2 핵심: 방향성 게이트 추가)

판정: 각 변형을 **같은 방향(long) 매칭 귀무**와 대조. BTC 우상향이라 랜덤 롱도 드리프트로
벌므로 **부풀린 귀무를 넘어야** 엣지다. 복수 구간에서 일관되게 넘어야 채택(한 구간만이면 커브핏).

돌리기:  python3 -m research.exp_A2_trend_regime
"""
from __future__ import annotations

import datetime as dt
import sys

import numpy as np

from engine.candles import TIMEFRAME_MINUTES
from research import lib
from research.exp_C_taker_delta import _median_hold_bars

SYMBOL = "BTCUSDT"
TIMEFRAME = "15m"          # edge-research 가설 A 와 같은 조건에서 재검증
LEVERAGE = 1
EQUITY_PCT = 10            # null_model 의 size_fraction 과 맞춘다(공정 비교)

# 강한 상승 구간(exp_C_regime 과 동일 — 같은 잣대로 비교되게).
WINDOWS = [
    ("2020-21 불장", "2020-10-01", "2021-04-14"),
    ("2023-24 불장", "2023-10-01", "2024-03-14"),
    ("2024 후반",    "2024-09-01", "2024-12-05"),
]

_ST = {"indicator": "SUPERTREND_DIR", "period": 10, "params": {"multiplier": 3.0}}


def _ms(s: str) -> int:
    return int(dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _preset(name: str, adx: float = None, di: bool = False) -> dict:
    """롱온리 flip 프리셋. adx=임계(None이면 게이트 없음), di=True면 +DI>-DI 방향성 게이트."""
    children = [{"cross": "crossOver", "left": _ST, "right": 0}]
    if adx is not None:
        children.append({"left": {"indicator": "ADX", "period": 14}, "cmp": ">", "right": adx})
    if di:
        children.append({"left": {"indicator": "PLUS_DI", "period": 14}, "cmp": ">",
                         "right": {"indicator": "MINUS_DI", "period": 14}})
    return {
        "schemaVersion": "1.0",
        "name": name,
        "market": {"exchange": "binance-futures", "symbol": SYMBOL,
                   "timeframe": TIMEFRAME, "direction": "long"},
        "entryRules": [{"side": "long", "when": {"op": "AND", "children": children}}],
        "entry": {"op": "AND", "children": children},
        "exit": {"stopLoss": {"type": "atrMultiple", "value": 2.0},
                 "takeProfit": {"type": "riskReward", "value": 2.0},
                 "timeStop": {"maxBars": 30}},
        "sizing": {"leverage": LEVERAGE, "marginMode": "isolated",
                   "size": {"type": "equityPercent", "value": EQUITY_PCT},
                   "minLiquidationBuffer": 10, "maxConcurrentPositions": 1},
        "filter": {"cooldownBars": 2, "avoidFundingWindowMinutes": 5},
    }


VARIANTS = [
    ("① flip 만",              _preset("flip", None, False)),
    ("② flip+ADX>25",          _preset("flip+adx", 25, False)),
    ("③ flip+ADX>25 & +DI>-DI", _preset("flip+adx+di", 25, True)),
]


def main():
    windows = WINDOWS
    if len(sys.argv) > 1:                     # 스모크: 구간 하나만
        windows = WINDOWS[:int(sys.argv[1])]
    tf_min = TIMEFRAME_MINUTES[TIMEFRAME]
    verdicts = {}

    for wname, a, b in windows:
        base, fsched = lib.load(SYMBOL, start_ms=_ms(a), end_ms=_ms(b))
        bh = (base.close[-1] / base.close[0] - 1) * 100
        print(f"\n{'='*100}\n[{wname}] {SYMBOL} {TIMEFRAME} · {a}~{b} · "
              f"{len(base):,}봉 · buy&hold {bh:+.1f}%")
        print(f"{'변형(롱온리)':<26} {'수익률':>8}  {'트레이드':>6}  {'승률':>6}  {'PF':>7}  {'MDD':>7}  {'수수료':>8}")
        print("-" * 100)

        for label, preset in VARIANTS:
            m = lib.backtest(base, preset, SYMBOL, funding_schedule=fsched)
            lib.show(label, m)
            if m.num_trades < 5:
                print("    ↳ 트레이드 부족(<5), 귀무 판정 생략")
                verdicts.setdefault(label, []).append(None)
                continue
            hold = _median_hold_bars(m, tf_min)
            nd = lib.null_model(base, TIMEFRAME, n_trades=m.num_trades, hold_bars=hold,
                                side="long", leverage=LEVERAGE, size_fraction=EQUITY_PCT / 100.0)
            v = lib.verdict(m.total_return_pct, nd)
            print(f"    ↳ 귀무: 전략 {v['strategy%']:+.2f}%  vs  랜덤롱 p95 {v['null_p95%']:+.2f}%"
                  f" (중앙값 {v['null_median%']:+.2f}%)  →  {v['verdict']}")
            verdicts.setdefault(label, []).append(v["beats_null"])

    print(f"\n{'='*100}\n[일관성] 복수 상승 구간에서 '일관되게' 넘어야 레짐 조건부 엣지 (한 구간만이면 커브핏)")
    for label, res in verdicts.items():
        hit = sum(1 for r in res if r)
        total = sum(1 for r in res if r is not None)
        mark = "✅ 채택 후보" if total and hit == total else ("⚠️ 일부만(커브핏 의심)" if hit else "❌ 엣지 아님")
        print(f"  {label:<26} {hit}/{total} 구간 통과   {mark}")
    print("\n결과를 BACKLOG.md 의 A2 '결과' 칸에 적을 것.\n")


if __name__ == "__main__":
    main()
