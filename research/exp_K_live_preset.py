"""K — 실제로 돌리고 있는 라이브 프리셋에 귀무모델 판정.

가설: SuperTrend flip 계열은 C·A2 에서 두 번 기각됐다. 하지만 **지금 테스트넷에서 돌고
실돈 후보인 프리셋**은 그 둘과 다르다 — 파라미터가 최적화됐고(14/2.5), HawkEye·QQE 필터가
붙었고, BTCUSDC(maker 0%)에서 롱·숏 동시로 돈다. 300일 백테스트가 +8% 대를 낸다.
그 +8% 가 랜덤 진입의 95%선을 넘는가?

★ 이 프리셋은 지금까지 판정을 안 거친 유일한 것이다. 연구 폴더 전체에 HAWKEYE·QQE 가
   한 번도 등장하지 않는다. 실돈을 걸기 전에 답해야 할 질문.

수수료 두 갈래로 돌린다 — 지금 테스트넷 검증이 재고 있는 바로 그 불확실성이다:
  maker  : 지정가가 체결된다는 백테스트 가정(BTCUSDC maker 0%)
  taker  : maker 가 실패해 전부 시장가로 나가는 경우(5bp)
★ 어느 쪽이든 **귀무에도 같은 수수료를 먹인다**. 전략만 싸게 하면 부당하게 이긴다(F 의 교훈).

롱·숏 동시라 귀무도 섞어 만든다: 관측된 롱 트레이드 수만큼의 랜덤 롱과 숏 트레이드 수만큼의
랜덤 숏을 각각 복리로 굴려 곱한다(순서는 복리 결과에 영향이 없다).

    python3 -m research.exp_K_live_preset [DAYS]
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

from research import lib

SYMBOL, TIMEFRAME = "BTCUSDC", "15m"
SAVED = "presets/saved/슈퍼트렌드_최적화_동적_레버리지.json"
SEEDS = (0, 1, 2, 3, 4)        # 귀무 p95 의 추정잡음을 재려고 여러 시드 (B 에서 이게 결론을 뒤집었다)
SAMPLES = 20000


def live_preset() -> dict:
    """라이브 프리셋. presets/saved 는 gitignore 라 없으면 내장 사본으로 폴백한다."""
    if os.path.exists(SAVED):
        return json.load(open(SAVED, encoding="utf-8"))["preset"]
    raise SystemExit(f"{SAVED} 없음 — 대시보드에서 저장한 프리셋이 필요하다")


def hold_bars_of(m, tf_min: int) -> int:
    """전략의 평균 보유 시간(신호봉 수) — 귀무의 보유 길이를 여기 맞춘다."""
    hs = [(t.exit_time - t.entry_time) / (tf_min * 60_000) for t in m.trades if t.exit_time]
    return max(1, int(round(float(np.mean(hs))))) if hs else 1


def one_way_fee(m) -> float:
    """전략이 실제로 낸 편도 수수료율 = 총수수료 / (진입명목+청산명목). 귀무에 같은 값을 준다."""
    notional = sum((t.entry_price + t.exit_price) * t.qty for t in m.trades)
    return (m.total_fees / notional) if notional > 0 else 0.0


def mixed_null(base, n_long, n_short, hold, lev, frac, fee, seed):
    """롱·숏을 관측 비율대로 섞은 귀무분포. 각 방향을 복리로 굴려 곱한다."""
    parts = []
    for n, side in ((n_long, "long"), (n_short, "short")):
        if n <= 0:
            continue
        d = lib.null_model(base, TIMEFRAME, n_trades=n, hold_bars=hold, side=side,
                           leverage=lev, size_fraction=frac, samples=SAMPLES, seed=seed, fee=fee)
        parts.append(1.0 + d / 100.0)
    if not parts:
        return np.zeros(1)
    out = parts[0]
    for p in parts[1:]:
        out = out * p
    return (out - 1.0) * 100.0


def main():
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 300
    d = live_preset()
    base, fsched = lib.load(SYMBOL, days=days)
    tf_min = lib.TIMEFRAME_MINUTES[TIMEFRAME]
    span = (base.open_time[-1] - base.open_time[0]) / 86_400_000
    print(f"K · {SYMBOL} {TIMEFRAME} · {span:.0f}일 · 봉 {len(base):,}")
    print(f"  진입: SuperTrend(14,2.5) 전환 + HawkEye(200,1.5) + QQE_MOD · 롱숏 동시\n")

    for label, mk, tk in (("maker 체결(백테스트 가정)", 0.0000, 0.0005),
                          ("전부 taker(maker 실패)", 0.0005, 0.0005)):
        m = lib.backtest(base, d, SYMBOL, funding_schedule=fsched, maker_fee=mk, taker_fee=tk)
        n_long = sum(1 for t in m.trades if t.side == 1)
        n_short = m.num_trades - n_long
        hold = hold_bars_of(m, tf_min)
        lev = int(round(float(np.mean([t.leverage for t in m.trades])))) if m.trades else 1
        frac = float((d.get("sizing", {}).get("size", {}) or {}).get("value", 10)) / 100.0
        fee = one_way_fee(m)

        print(f"── {label}")
        lib.show("  전략", m)
        print(f"  귀무 매칭: 롱 {n_long} · 숏 {n_short} · 보유 {hold}봉 · lev {lev} · "
              f"명목비율 {frac:.0%} · 편도수수료 {fee*100:.4f}%")
        p95s = []
        for seed in SEEDS:
            dist = mixed_null(base, n_long, n_short, hold, lev, frac, fee, seed)
            p95s.append(float(np.percentile(dist, 95)))
        dist = mixed_null(base, n_long, n_short, hold, lev, frac, fee, 0)
        v = lib.verdict(m.total_return_pct, dist)
        lo, hi = min(p95s), max(p95s)
        # 백분위가 p95 초과 여부보다 많은 걸 말해준다 — '아깝게 미달'과 '한참 아래'는 다른 얘기다.
        pctile = float((dist < m.total_return_pct).mean() * 100.0)
        print(f"  귀무 중앙 {np.median(dist):+.2f}%  p95 {np.mean(p95s):+.2f}%  "
              f"(시드별 {lo:+.2f}~{hi:+.2f}, 폭 {hi-lo:.2f}%p)")
        margin = m.total_return_pct - float(np.mean(p95s))
        print(f"  전략은 귀무분포의 상위 {100-pctile:.0f}% 지점 (백분위 {pctile:.0f})")
        print(f"  초과폭 {margin:+.2f}%p → {v['verdict']}"
              f"{'  ⚠ 초과폭이 시드잡음보다 작다(판정 불능)' if 0 < margin < (hi - lo) else ''}\n")


if __name__ == "__main__":
    main()
