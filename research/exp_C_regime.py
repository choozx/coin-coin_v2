"""[C] 오더플로우 필터 — 상승/추세 구간 OOS 재검증 (BACKLOG.md C의 '진짜 판정').

스모크(exp_C_taker_delta)는 하락구간이라 '필터=출혈감소'만 봤다. 진짜 질문은:
  상승장에서 델타 필터가 '랜덤 롱(이미 BTC 드리프트로 버는)'을 넘는 엣지를 만드는가?
→ 강한 상승 구간을 잘라 롱온리로 델타 유/무를 돌리고, 각 결과를 **같은 방향(long) 귀무**와 대조.
  상승장 롱은 드리프트로 부풀린 귀무를 **넘어야** 엣지다(edge-research 판정 프레임).

돌리기:  python3 -m research.exp_C_regime
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import os

import numpy as np

from engine.candles import TIMEFRAME_MINUTES
from research import lib
from research.exp_C_taker_delta import PRESET_PATH, _median_hold_bars, _variant

# 강한 상승 구간(buy&hold로 확인함). OOS = 이 구간들은 하락장 스모크와 다른 데이터.
WINDOWS = [
    ("2020-21 불장", "2020-10-01", "2021-04-14"),
    ("2023-24 불장", "2023-10-01", "2024-03-14"),
    ("2024 후반",    "2024-09-01", "2024-12-05"),
]
THRESHOLDS = (None, 0.2, 0.3)          # 필터 없음 / 0.2 / 0.3


def _ms(s):
    return int(dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def _long_only(preset: dict) -> dict:
    """숏 진입규칙 제거 — 상승장에선 롱 엣지만 본다(숏은 드리프트 역행으로 출혈)."""
    p = copy.deepcopy(preset)
    p["entryRules"] = [r for r in p.get("entryRules", []) if r.get("side") == "long"]
    p["market"]["direction"] = "long"
    return p


def main():
    with open(PRESET_PATH) as f:
        base_preset = json.load(f)
    base_preset["market"]["symbol"] = "BTCUSDT"
    tf = base_preset["market"]["timeframe"]
    lev = base_preset["sizing"]["leverage"]
    frac = base_preset["sizing"]["size"]["value"] / 100.0

    for name, a, b in WINDOWS:
        base, fsched = lib.load("BTCUSDT", start_ms=_ms(a), end_ms=_ms(b))
        bh = (base.close[-1] / base.close[0] - 1) * 100
        print(f"\n{'='*96}\n[{name}] BTCUSDT {tf} · {a}~{b} · {len(base):,}봉 · buy&hold {bh:+.1f}%")
        print(f"{'변형(롱온리)':<24} {'수익률':>8}  {'트레이드':>6}  {'승률':>6}  {'PF':>7}  {'MDD':>7}  {'수수료':>8}")
        print("-" * 96)

        for thr in THRESHOLDS:
            preset = _long_only(_variant(base_preset, thr))
            m = lib.backtest(base, preset, "BTCUSDT", funding_schedule=fsched)
            lib.show(f"델타 {'없음' if thr is None else '≥'+str(thr)}", m)

            if m.num_trades >= 5:
                hold = _median_hold_bars(m, TIMEFRAME_MINUTES[tf])
                nd = lib.null_model(base, tf, n_trades=m.num_trades, hold_bars=hold,
                                    side="long", leverage=lev, size_fraction=frac)
                v = lib.verdict(m.total_return_pct, nd)
                print(f"    ↳ 귀무 판정: 전략 {v['strategy%']:+.1f}%  vs  랜덤롱 p95 {v['null_p95%']:+.1f}%"
                      f" (중앙값 {v['null_median%']:+.1f}%)  →  {v['verdict']}")
            else:
                print("    ↳ 트레이드 부족, 귀무 판정 생략")

    print(f"\n{'='*96}")
    print("정리: 어느 구간·임계든 전략이 '랜덤롱 p95'를 넘어야 오더플로우 필터가 엣지. "
          "못 넘으면 상승장에서도 필터는 드리프트 편승일 뿐 → C 기각.")
    print("결과를 BACKLOG.md C 항목에 회기할 것.\n")


if __name__ == "__main__":
    main()
