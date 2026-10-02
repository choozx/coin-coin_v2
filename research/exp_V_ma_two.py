"""V — 영상 기법 'SMA200 으로 방향, SMA22 로 타점' (이마누엘 매매법 유튜브 대본, 2026-10-02 사용자 제공).

영상 규칙을 기계적으로 옮긴 것(재량 판단·'세력 지표'는 뺐다 — 계산식 비공개, 재현 불가):

  방향   롱만: 종가 > SMA200 이고 SMA200 이 우상향  /  숏만: 종가 < SMA200 이고 우하향
  횡보   직전 W 봉 교차(가격×SMA22 · SMA22×SMA200 · 가격×SMA200) ≥ 4 → 매매 안 함 ('4단 교차')
  돌파   종가가 SMA22 를 넘고(롱) 양봉이며, 캔들이 SMA22 에 안 닿거나(저가 > SMA22) 몸통이 1.5 ATR 이상
  스퀴즈 (변형) 직전 10봉 안에 SMA22·SMA200 간격이 g% 미만이었던 적이 있다 — 영상의 '스퀴즈 플레이'
  청산   몸통이 SMA22 반대편에서 마감(롱: 종가 < SMA22)  ·  손절 = 진입 캔들 부근 저점(swing 2봉)

영상이 숫자를 정한 것(200·22·4회·롱숏 양방향)은 고정. **숫자를 안 준 것만** 몇 값으로 본다:
  기울기 판정 봉수 L ∈ {10, 20} · 횡보 창 W ∈ {20, 40} · 스퀴즈 g ∈ {없음, 0.5%, 1.0%}  → 12 × TF 2 = 24

TF 는 사용자 지정 15m·30m(영상 설명은 30m·1h, 단타 예시만 5m).
판정은 T·U 와 같다(결과 전 고정): 구간1·2 선택(best-of-24 p95) → 하나만 홀드아웃 1회.
레버리지 1·명목 100%. 영상은 '비트코인은 레버리지를 높여도 된다'지만 레버리지는 엣지를 만들지 않는다.

    python3 -u -m research.exp_V_ma_two
    python3 -u -m research.exp_V_ma_two --zero-fee
"""
from __future__ import annotations

import itertools
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from research import lib                                       # noqa: E402
from research.exp_T_supertrend_search import _ym, segments     # noqa: E402
from research.exp_U_vwap_band import _null                     # noqa: E402

SYMBOL = "BTCUSDT"
ZERO_FEE = "--zero-fee" in sys.argv
TFS = ("15m", "30m")
SLOPE_LB = (10, 20)
CHOP_W = (20, 40)
SQUEEZE = (None, 0.5, 1.0)
GRID = list(itertools.product(TFS, SLOPE_LB, CHOP_W, SQUEEZE))

_W = {}
CLOSE, OPEN, LOW, HIGH = ({"source": s} for s in ("close", "open", "low", "high"))
SMA22 = {"indicator": "SMA", "period": 22}
SMA200 = {"indicator": "SMA", "period": 200}


def build(tf, lb, w, g) -> dict:
    slope = {"indicator": "SMA_SLOPE", "period": 200, "params": {"lookback": lb}}
    chop = {"indicator": "MA_CROSSES", "period": 200, "params": {"fast": 22, "slow": 200, "window": w}}
    body = {"indicator": "BODY_ATR", "period": 14}

    def side_rule(long: bool):
        gt, lt = (">", "<") if long else ("<", ">")
        kids = [
            {"left": CLOSE, "cmp": gt, "right": SMA200},                     # 방향: 200선 위/아래
            {"left": slope, "cmp": gt, "right": 0},                          # 방향: 200선 머리
            {"left": chop, "cmp": "<", "right": 4},                          # 횡보 아님
            {"left": CLOSE, "cmp": gt, "right": SMA22},                      # 22선 돌파
            {"left": CLOSE, "cmp": gt, "right": OPEN},                       # 방향 맞는 캔들
            {"op": "OR", "children": [                                       # 돌파 확정 두 기준
                {"left": LOW if long else HIGH, "cmp": gt, "right": SMA22},  #  ② 선에 안 닿음
                {"left": body, "cmp": ">", "right": 1.5}]},                  #  ① 몸통이 긴 캔들
        ]
        if g:
            kids.append({"left": {"indicator": "MA_GAP", "period": 200,
                                  "params": {"fast": 22, "slow": 200, "window": 10}},
                         "cmp": "<", "right": g})                            # 스퀴즈 이후
        return {"side": "long" if long else "short", "when": {"op": "AND", "children": kids}}

    rules = [side_rule(True), side_rule(False)]
    return {
        "schemaVersion": "1.0", "name": f"SMA200/22 {tf}",
        "market": {"exchange": "binance-futures", "symbol": SYMBOL, "timeframe": tf, "direction": "both"},
        "entry": {"op": "OR", "children": [r["when"] for r in rules]},
        "entryRules": rules,
        "exit": {
            "conditionBySide": {"long": {"left": CLOSE, "cmp": "<", "right": SMA22},
                                "short": {"left": CLOSE, "cmp": ">", "right": SMA22}},
            "stopLoss": {"type": "swing", "lookback": 2},
        },
        "sizing": {"leverage": 1, "marginMode": "isolated",
                   "size": {"type": "equityPercent", "value": 100.0}},
        "filter": {"cooldownBars": 1},
    }


def _label(c):
    tf, lb, w, g = c
    return f"{tf:>3} 기울기{lb}봉 횡보창{w} 스퀴즈{g or '없음'}"


def _init(seg_idx, zero_fee):
    _W["fee"] = 0.0 if zero_fee else None
    segs = segments()
    _W["data"] = {i: lib.load(SYMBOL, start_ms=segs[i][0], end_ms=segs[i][1]) for i in seg_idx}


def _run(args):
    combo, i = args
    base, fs = _W["data"][i]
    f = _W["fee"]
    return combo, i, lib.backtest(base, build(*combo), SYMBOL, funding_schedule=fs,
                                  maker_fee=f, taker_fee=f)


def main() -> int:
    segs = segments()
    workers = max(1, (os.cpu_count() or 2) - 1)
    fee = 0.0 if ZERO_FEE else None
    print(f"V · SMA200 방향 + SMA22 타점 · {SYMBOL} · TF {'/'.join(TFS)} · 격자 {len(GRID)} · "
          f"수수료 {'0 (상한)' if ZERO_FEE else '전부 taker'}")
    for i, (a, b) in enumerate(segs):
        print(f"  구간{i+1}: {_ym(a)} ~ {_ym(b)}{'  (홀드아웃 — 선택 후 1회)' if i == 2 else ''}")

    res = {}
    jobs = [(c, i) for c in GRID for i in (0, 1)]
    with ProcessPoolExecutor(min(workers, len(jobs)), initializer=_init,
                             initargs=((0, 1), ZERO_FEE)) as ex:
        for c, i, m in ex.map(_run, jobs):
            res[(c, i)] = m

    data = {i: lib.load(SYMBOL, start_ms=segs[i][0], end_ms=segs[i][1]) for i in (0, 1)}
    print(f"\n  {'조합':34} {'구간1':>9} {'거래':>5} {'승률':>5} {'구간2':>9} {'거래':>5} {'승률':>5}   best-of-{len(GRID)} p95")
    passed = []
    order = sorted(GRID, key=lambda c: -min(res[(c, 0)].total_return_pct, res[(c, 1)].total_return_pct))
    for c in order:
        m0, m1 = res[(c, 0)], res[(c, 1)]
        g = [_null(res[(c, i)], data[i][0], c[0], len(GRID)) for i in (0, 1)]
        ok = m0.total_return_pct > 0 and m1.total_return_pct > 0 and all(x["beatsBestOfN"] for x in g)
        if ok:
            passed.append(c)
        print(f"  {_label(c):34} {m0.total_return_pct:+8.1f}% {m0.num_trades:5d} {m0.win_rate_pct:4.0f}% "
              f"{m1.total_return_pct:+8.1f}% {m1.num_trades:5d} {m1.win_rate_pct:4.0f}%   "
              f"{g[0]['bestOfNP95']:+7.1f} / {g[1]['bestOfNP95']:+7.1f}  {'✅' if ok else '❌'}", flush=True)

    if not passed:
        print("\n  ★ 결론: 사전 규칙 ①을 통과한 조합이 없다 → 홀드아웃을 열지 않는다.")
        return 0
    pick = passed[0]
    print(f"\n  ★ 선택: {_label(pick)} → 홀드아웃 1회")
    base3, fs3 = lib.load(SYMBOL, start_ms=segs[2][0], end_ms=segs[2][1])
    m3 = lib.backtest(base3, build(*pick), SYMBOL, funding_schedule=fs3, maker_fee=fee, taker_fee=fee)
    g3 = _null(m3, base3, pick[0], 1)
    lib.show("  홀드아웃", m3)
    print(f"  귀무 중앙 {g3['nullMedian']:+.1f}% · p95 {g3['nullP95']:+.1f}% · 백분위 {g3['percentile']:.0f}")
    print(f"  판정: {'✅ 통과' if g3['beatsNull'] and m3.total_return_pct > 0 else '❌ 홀드아웃 실패'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
