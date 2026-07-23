"""[C] 오더플로우 필터 (테이커델타/CVD) — BACKLOG.md 의 C 항목.

가설: flip 진입에 per-trade 엣지가 없어 매매가 수수료만 낸다. 진입에 테이커 매수우위
(TAKER_DELTA_RATIO) 확인을 걸면 가짜 신호가 걸러져 트레이드·수수료↓, 남은 트레이드 품질↑.

이 스크립트가 하는 것:
  1) 델타 임계 없음/0.2/0.3/0.5 변형을 같은 구간에 백테스트해 트레이드수·PF·순수익 비교
  2) '필터 없음' 변형을 매칭 귀무모델과 대조 → per-trade 엣지 유무 판정

돌리기:  python3 -m research.exp_C_taker_delta [SYMBOL] [DAYS]
예:      python3 -m research.exp_C_taker_delta BTCUSDT 180

주의: 캐시 전용. taker_buy 백필 필요 — `python3 -m engine.candle_store --backfill-taker BTCUSDT`.
BACKLOG 의 판정기준은 '상승/추세 구간'에서의 OOS 귀무 초과다. 아래 기본 구간(최근 N일)은
빠른 스모크용 — 구간을 바꿔(load 의 start_ms/end_ms) 추세장에서 재검증할 것.
"""
from __future__ import annotations

import copy
import json
import os
import sys

import numpy as np

from research import lib

PRESET_PATH = os.path.join(os.path.dirname(__file__), "..", "presets", "examples",
                           "supertrend-flip-delta-5m.json")


def _variant(base_preset: dict, thr):
    """델타 임계 thr 로 변형. thr=None 이면 델타 조건 제거(=필터 없음)."""
    p = copy.deepcopy(base_preset)

    def patch(children, side):
        out = []
        for c in children:
            ind = (c.get("left") or {}).get("indicator")
            if ind == "TAKER_DELTA_RATIO":
                if thr is None:
                    continue                                  # 조건 삭제 = 필터 없음
                c = copy.deepcopy(c)
                c["right"] = thr if side == "long" else -thr
            out.append(c)
        return out

    for rule in p.get("entryRules", []):
        rule["when"]["children"] = patch(rule["when"]["children"], rule["side"])
    if "entry" in p:                                          # 폴백 트리(롱 기준)
        p["entry"]["children"] = patch(p["entry"]["children"], "long")
    p["name"] = f"flip+델타{'없음' if thr is None else thr}"
    return p


def _median_hold_bars(m, tf_min: int) -> int:
    if not m.trades:
        return 30
    holds = [(t.exit_time - t.entry_time) / (tf_min * 60000) for t in m.trades]
    return max(1, int(np.median(holds)))


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    days = float(sys.argv[2]) if len(sys.argv) > 2 else 180.0

    with open(PRESET_PATH) as f:
        base_preset = json.load(f)
    base_preset["market"]["symbol"] = symbol
    tf = base_preset["market"]["timeframe"]

    base, fsched = lib.load(symbol, days=days)
    print(f"\n[C] {symbol} {tf} · 최근 {days:.0f}일 · {len(base):,} 1분봉")
    print(f"{'변형':<28} {'수익률':>8}  {'트레이드':>6}  {'승률':>6}  {'PF':>7}  {'MDD':>7}  {'수수료':>8}  {'펀딩':>7}")
    print("-" * 96)

    runs = {}
    for thr in (None, 0.2, 0.3, 0.5):
        m = lib.backtest(base, _variant(base_preset, thr), symbol, funding_schedule=fsched)
        lib.show(f"델타 {'없음' if thr is None else '≥'+str(thr)}", m)
        runs[thr] = m

    # 판정: '필터 없음' 변형이 우연을 넘는가? (per-trade 엣지 유무)
    from engine.candles import TIMEFRAME_MINUTES
    m0 = runs[None]
    if m0.num_trades >= 5:
        hold = _median_hold_bars(m0, TIMEFRAME_MINUTES[tf])
        nd = lib.null_model(base, tf, n_trades=m0.num_trades, hold_bars=hold,
                            side="long", leverage=base_preset["sizing"]["leverage"],
                            size_fraction=base_preset["sizing"]["size"]["value"] / 100.0)
        v = lib.verdict(m0.total_return_pct, nd)
        print("\n귀무모델 판정 (필터 없음 변형, 같은 트레이드수·보유·롱 기준):")
        for k, val in v.items():
            print(f"  {k:<14} {val}")
    else:
        print("\n트레이드가 너무 적어 귀무 판정 생략.")

    print("\n결론 메모 → BACKLOG.md 의 C 항목 '결과' 칸에 적어둘 것.")
    print("다음 단계: load(start_ms,end_ms) 로 상승/추세 구간을 잘라 OOS 귀무 초과 여부를 볼 것.\n")


if __name__ == "__main__":
    main()
