"""S — 손절이 꼬리를 얼마나 자르나 (라이브 프리셋).

★ 이건 '수익을 만들 수 있나' 를 묻는 실험이 **아니다.** K 가 이 프리셋을 이미 기각했다
(귀무 p95 미달). 여기서 묻는 건 리스크 하나다:

    레버리지 10~20배에서 **손절 없이** 도는 게 얼마나 위험한가, 손절이 그걸 얼마나 줄이나.

지금 프리셋의 청산 수단은 SuperTrend 전환 하나뿐이다(exit 에 stopLoss 가 없다). 전환
신호가 나올 때까지 버티므로, 급락 한 번이 증거금을 통째로 가져갈 수 있다. 잔고 1만이면
티어상 **20배**라 청산가가 **약 5% 거리**다 — 그보다 먼 손절은 걸어봐야 의미가 없다.

방법: 손절만 바꿔가며 같은 캔들·같은 신호로 돌리고 **모양**을 본다.
  ⚠️ 한 값이 좋다고 그걸 고르지 않는다. 이웃 값이 나쁘면 그건 커브핏이다.
     손절은 '최적화 대상' 이 아니라 '얼마를 잃고 나갈 것인가' 라는 정책이다.

수수료는 새 기본(maker_fill_ratio=0, 전부 taker)을 쓴다 — 손절은 거래를 **늘리므로**
수수료 가정이 낙관적이면 손절이 실제보다 좋아 보인다.

    python3 -m research.exp_S_stoploss              # BTCUSDC 300일 (실제 매매 심볼)
    python3 -m research.exp_S_stoploss --segments   # BTCUSDT 독립 3구간 (커브핏 판별)

★ 왜 구간을 나누나: 한 구간에서 좋아 보이는 손절값은 거의 항상 잡음이다. 이 프로젝트에서
   그 함정에 이미 두 번 빠졌다(L 의 회전율 오독, B 의 통과 2건). **세 구간 공통 통과**가
   아니면 채택하지 않는다 — 그게 여기 판정 기준이다.
"""
from __future__ import annotations

import copy
import sys

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import binance_math as bm, candle_store          # noqa: E402
from engine.backtest import BacktestConfig, run              # noqa: E402
from engine.preset import Preset, load_preset_file           # noqa: E402

PRESET = "presets/saved/슈퍼트렌드_최적화_동적_레버리지.json"
EQUITY = 10_000.0


def variants():
    """(라벨, stopLoss dict or None). 퍼센트는 청산 거리(≈5%)를 넘지 않게 잡는다."""
    out = [("손절 없음(현행)", None)]
    for v in (0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0):
        out.append((f"percent {v}%", {"type": "percent", "value": v}))
    for v in (1.0, 1.5, 2.0, 3.0):
        out.append((f"ATR × {v}", {"type": "atrMultiple", "value": v, "atrPeriod": 14}))
    return out


def build(base_preset: Preset, stop) -> Preset:
    d = copy.deepcopy(base_preset.data)
    ex = d.setdefault("exit", {})
    if stop is None:
        ex.pop("stopLoss", None)
    else:
        ex["stopLoss"] = stop
    return Preset.from_dict(d, validate=False)


def worst_trade_pct(m) -> float:
    """최악 단일 트레이드 손실(초기자본 대비 %). 꼬리의 크기다."""
    if not m.trades:
        return 0.0
    return min(t.pnl for t in m.trades) / EQUITY * 100


def _table(p, base, mk, tk, title):
    print(f"\n  {title}")
    print(f"  {'손절':16} {'수익률':>9} {'MDD':>8} {'최악거래':>9} {'청산':>5} "
          f"{'손절체결':>7} {'거래':>5} {'승률':>6}")
    print("  " + "─" * 78)
    out = {}
    for label, stop in variants():
        cfg = BacktestConfig(initial_equity=EQUITY, maker_fee=mk, taker_fee=tk)
        m = run(base, build(p, stop), cfg)
        liq = sum(1 for t in m.trades if t.exit_reason == "liquidation")
        sl = sum(1 for t in m.trades if t.exit_reason == "stop_loss")
        out[label] = (m.total_return_pct, m.max_drawdown_pct, worst_trade_pct(m), liq)
        print(f"  {label:16} {m.total_return_pct:+8.2f}% {m.max_drawdown_pct:7.2f}% "
              f"{worst_trade_pct(m):+8.2f}% {liq:5d} {sl:7d} {m.num_trades:5d} "
              f"{m.win_rate_pct:5.1f}%")
    return out


def _segments(p) -> int:
    """독립 3구간 — 한 구간의 '좋은 손절값'이 다른 구간에서도 좋은가."""
    sym = "BTCUSDT"                       # 6.9년. 손절은 심볼이 아니라 정책 문제다.
    mk, tk = bm.fees_for_symbol(sym)
    st = candle_store.stats(sym)          # 테이블 이름을 직접 알 필요 없다(스토어 API)
    lo, hi = st["min"], st["max"]
    if not lo or not hi:
        print(f"{sym} 캐시 없음 — 먼저 수집하세요"); return 1
    span = (hi - lo) // 3
    print(f"S · 손절 정책 · {sym} · 독립 3구간 · maker {mk*1e4:.0f}bp / taker {tk*1e4:.0f}bp")
    print("  수수료: 전부 taker(maker_fill_ratio=0) — 손절은 거래를 늘리므로 "
          "낙관적 가정이면 손절이 좋아 보인다")
    tables = []
    for i in range(3):
        base = candle_store.load_range(sym, lo + i * span, lo + (i + 1) * span)
        tables.append(_table(p, base, mk, tk, f"구간 {i+1}/3 · {len(base):,}봉"))
    # 세 구간 공통으로 '손절 없음'보다 나은 값만 후보다.
    print("\n  ── 세 구간 공통 판정 ──")
    baseline = [t["손절 없음(현행)"][0] for t in tables]
    winners = [lab for lab, _ in variants() if lab != "손절 없음(현행)"
               and all(tables[i][lab][0] > baseline[i] for i in range(3))]
    print(f"    손절 없음 수익률: {['%+.1f%%' % b for b in baseline]}")
    print(f"    세 구간 모두 그보다 나은 손절: {winners or '없음'}")
    liq_free = all(t["손절 없음(현행)"][3] == 0 for t in tables)
    print(f"    손절 없이 청산당한 구간: {'없음' if liq_free else '있음'}")
    return 0


def main() -> int:
    p = load_preset_file(PRESET, validate=True)
    if "--segments" in sys.argv:
        return _segments(p)
    days = float(sys.argv[1]) if len(sys.argv) > 1 else 300
    sym = p.symbol
    base = candle_store.load_recent(sym, days=days)
    mk, tk = bm.fees_for_symbol(sym)
    print(f"S · 손절 유무 · {sym} · {len(base):,}봉({days:.0f}일) · "
          f"maker {mk*1e4:.0f}bp / taker {tk*1e4:.0f}bp")
    print("  수수료 가정: 전부 taker(maker_fill_ratio=0)")
    _table(p, base, mk, tk, "단일 구간")
    print("\n  읽는 법: 수익률이 아니라 **청산·최악거래·MDD** 를 본다. 이 전략은 이미 기각됐고,")
    print("           손절의 값어치는 '덜 번다'가 아니라 '한 방에 안 죽는다' 에 있다.")
    print("           한 구간 결과만으로 손절값을 고르지 말 것 → --segments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
