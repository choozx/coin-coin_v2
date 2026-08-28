"""[F] 수수료 체계를 바꾸면 엣지가 되는가 — C·A2·B 의 공통 벽에 대한 직격 검증.

C·A2·B 에서 반복된 패턴: 게이트를 조일수록 손실이 **0 으로 수렴하지만 양수로 못 넘어간다.**
= per-trade 기대값이 0 근처인데 수수료가 그걸 음수로 민다. 그렇다면 질문은 하나다 —
**수수료를 줄이면 그 0 근처 기대값이 양수로 넘어오는가?**

수수료 시나리오(BTCUSDT 왕복 기준):
  taker   진입·청산 모두 taker  0.05%+0.05% = 0.10%   ← 지금까지의 모든 실험
  혼합    진입 maker·청산 taker 0.02%+0.05% = 0.07%   ← post-only 진입이 체결됐을 때
  maker   진입·청산 모두 maker  0.02%+0.02% = 0.04%   ← 낙관적 상한
  USDC    BTCUSDC maker 0% 프로모션                    ← 실제 봇이 쓰는 심볼(별도 확인)

★ 정직성 두 가지:
 1) **귀무에도 같은 수수료를 먹인다.** 전략만 싸게 하면 부당하게 이긴다. 수수료가 싸지면
    랜덤 진입도 덜 잃으므로 문턱 자체가 올라간다 — 그래서 '수수료를 낮추면 이긴다'가
    자동으로 성립하지 않는다. 이 실험의 핵심이 바로 그 지점이다.
 2) **maker 는 낙관적 상한이다.** post-only 지정가는 미체결 위험이 있어 실제로는 일부만
    maker 로 채워진다. 여기서 안 되면 확실히 닫히고, 되면 그때 체결률을 따져야 한다.

돌리기:  python3 -m research.exp_F_maker_fees
"""
from __future__ import annotations

from engine.candles import TIMEFRAME_MINUTES
from research import lib
from research.exp_A2_trend_regime import _preset as a2_preset, _ms as a2_ms, WINDOWS
from research.exp_B_meanrevert import _preset as b_preset, _ms as b_ms, YEARS
from research.exp_C_taker_delta import _median_hold_bars

SYMBOL, TF, LEV, PCT = "BTCUSDT", "15m", 1, 10

# (라벨, 편도수수료) — 왕복은 2배. 귀무에도 같은 값을 먹인다.
FEE_CASES = [("taker 0.05%", 0.0005), ("혼합(진입maker)", 0.00035), ("maker 0.02%", 0.0002)]


def _judge(base, m, side, fee):
    """수수료 fee 를 먹인 매칭 귀무와 대조 → (전략%, p95%, 초과여부)."""
    if m.num_trades < 5:
        return m.total_return_pct, None, None
    hold = _median_hold_bars(m, TIMEFRAME_MINUTES[TF])
    nd = lib.null_model(base, TF, n_trades=m.num_trades, hold_bars=hold, side=side,
                        leverage=LEV, size_fraction=PCT / 100.0, fee=fee, samples=20000)
    v = lib.verdict(m.total_return_pct, nd)
    return v["strategy%"], v["null_p95%"], v["beats_null"]


def _row(tag, base, preset, side, fsched):
    """한 전략을 세 수수료 시나리오로 돌려 한 줄 출력 → 통과한 시나리오 라벨 목록."""
    cells, wins = [], []
    for label, fee in FEE_CASES:
        # taker_fee 를 시나리오 값으로 덮는다(진입·청산 전부 그 수수료로 친다는 뜻).
        m = lib.backtest(base, preset, SYMBOL, funding_schedule=fsched,
                         maker_fee=fee, taker_fee=fee)
        r, p95, beat = _judge(base, m, side, fee)
        if p95 is None:
            cells.append(f"{r:+6.2f}/  n/a  ")
        else:
            cells.append(f"{r:+6.2f}/{p95:+6.2f}{'✅' if beat else '❌'}")
            if beat:
                wins.append(label)
    print(f"  {tag:<30} " + "   ".join(cells))
    return wins


def main():
    print(f"\n{SYMBOL} {TF} · 레버리지{LEV} · 각 칸 = 전략%/귀무p95% (귀무도 같은 수수료)")
    hdr = "   ".join(f"{l:^15}" for l, _ in FEE_CASES)
    tally = {}

    print(f"\n{'='*100}\n[A2] 추세추종 flip+ADX>25 & +DI>-DI (롱, 상승 구간)")
    print(f"  {'구간':<30} {hdr}")
    for wname, a, b in WINDOWS:
        base, fs = lib.load(SYMBOL, start_ms=a2_ms(a), end_ms=a2_ms(b))
        tally[f"A2 {wname}"] = _row(wname, base, a2_preset("a2", 25, True), "long", fs)

    print(f"\n{'='*100}\n[B] 평균회귀 RSI+ADX<20 (연도별)")
    for side, gate_label in (("long", "롱 RSI<30+ADX<20"), ("short", "숏 RSI>70+ADX<20")):
        print(f"\n  {gate_label}")
        print(f"  {'연도':<30} {hdr}")
        for y in YEARS:
            base, fs = lib.load(SYMBOL, start_ms=b_ms(y), end_ms=b_ms(y + 1))
            tally[f"B {side} {y}"] = _row(str(y), base, b_preset(side, True), side, fs)

    print(f"\n{'='*100}\n[정리] 시나리오별 '귀무 초과' 건수 (전체 {len(tally)}건)")
    for label, _ in FEE_CASES:
        hits = [k for k, v in tally.items() if label in v]
        print(f"  {label:<16} {len(hits):>2}건" + (f"  ← {', '.join(hits)}" if hits else ""))
    print("\n수수료를 낮춰도 초과 건수가 안 늘면: 벽은 수수료가 아니라 트리거의 per-trade 기대값이다.")
    print("(귀무도 같이 싸지므로 문턱이 함께 올라간다 — 이게 이 실험의 요점)\n")


if __name__ == "__main__":
    main()
