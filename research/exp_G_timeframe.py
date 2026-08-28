"""[G] 시간축을 올리면 per-trade 기대값이 수수료를 넘는가 — C·A2·B·F 이후 남은 갈래.

지금까지 모든 실험은 **15m 인트라데이**였고 전부 같은 벽에 막혔다: per-trade 기대값이
0 근처라 랜덤 진입을 못 넘는다. F 에서 '수수료를 줄여도 귀무가 같이 싸져 격차 불변'임이
확인됐으니, 남은 길은 **트레이드당 기대 변동폭 자체를 키우는 것** — 즉 시간축을 올리는 것.

가설: 15m 에서 잡히는 움직임은 노이즈에 가깝고 수수료·스프레드에 먹힌다. 4h·1d 로 올리면
한 트레이드가 담는 변동폭이 커져 per-trade 기대값이 수수료를 넘어설 수 있다.

주의(정직성):
 - timeStop 은 **30봉 고정**이다. TF 를 올리면 실제 보유시간도 같이 늘어나는데, 그게 바로
   이 실험이 보려는 것(더 긴 호흡)이므로 의도적이다.
 - TF 를 올릴수록 트레이드가 급감한다 → 표본 부족. 그래서 연도별이 아니라 **전체 기간**
   (2019-09~2026-07, 6.9년)으로 돌리고 트레이드 5건 미만은 판정을 생략한다.
 - 귀무는 같은 TF·같은 보유봉수·같은 방향으로 매칭된다(hold_bars 는 전략 중앙값).

돌리기:  python3 -m research.exp_G_timeframe
"""
from __future__ import annotations

from engine.candles import TIMEFRAME_MINUTES
from research import lib
from research.exp_A2_trend_regime import _preset as a2_preset
from research.exp_B_meanrevert import _preset as b_preset
from research.exp_C_taker_delta import _median_hold_bars

SYMBOL, LEV, PCT = "BTCUSDT", 1, 10
TFS = ["15m", "1h", "4h", "1d"]
DAYS = 2500                        # 전체 캐시(2019-09~) 를 덮는 넉넉한 값


def _tf(preset: dict, tf: str) -> dict:
    p = dict(preset)
    p["market"] = dict(p["market"])
    p["market"]["timeframe"] = tf
    return p


STRATS = [
    ("A2 flip+ADX>25&+DI (롱)", lambda: a2_preset("a2", 25, True), "long"),
    ("B  RSI<30+ADX<20 (롱)",   lambda: b_preset("long", True),    "long"),
    ("B  RSI>70+ADX<20 (숏)",   lambda: b_preset("short", True),   "short"),
]


def main():
    base, fs = lib.load(SYMBOL, days=DAYS)
    span_d = (base.open_time[-1] - base.open_time[0]) / 86400_000
    print(f"\n{SYMBOL} · {len(base):,} 1분봉 · {span_d/365:.1f}년 · 레버리지{LEV} · 자본비중{PCT}%")
    print("각 칸 = 전략% / 귀무p95% · T=트레이드수 · 보유=중앙 보유봉수\n")

    for name, mk, side in STRATS:
        print("=" * 104)
        print(f"[{name}]")
        print(f"  {'TF':<6} {'수익률':>9} {'귀무p95':>9} {'판정':<6} {'T':>6} {'보유봉':>7} "
              f"{'승률':>7} {'PF':>7} {'수수료':>9} {'MDD':>7}")
        print("  " + "-" * 100)
        for tf in TFS:
            m = lib.backtest(base, _tf(mk(), tf), SYMBOL, funding_schedule=fs)
            s = lib.summarize(m)
            if m.num_trades < 5:
                print(f"  {tf:<6} {s['return%']:>+9.2f} {'n/a':>9} {'표본부족':<6} "
                      f"{m.num_trades:>6}")
                continue
            hold = _median_hold_bars(m, TIMEFRAME_MINUTES[tf])
            nd = lib.null_model(base, tf, n_trades=m.num_trades, hold_bars=hold, side=side,
                                leverage=LEV, size_fraction=PCT / 100.0, samples=20000)
            v = lib.verdict(m.total_return_pct, nd)
            mark = "✅" if v["beats_null"] else "❌"
            print(f"  {tf:<6} {v['strategy%']:>+9.2f} {v['null_p95%']:>+9.2f} {mark:<6} "
                  f"{m.num_trades:>6} {hold:>7} {s['win%']:>6.1f}% {str(s['pf']):>7} "
                  f"{s['fees']:>9.0f} {s['mdd%']:>6.2f}%")
        print()

    print("=" * 104)
    print("읽는 법: TF 를 올려도 전략과 귀무의 격차가 안 벌어지면, 벽은 시간축이 아니다.")
    print("         (수익률이 커져도 귀무가 같이 커지면 의미 없음 — F 에서 수수료가 그랬듯)\n")


if __name__ == "__main__":
    main()
