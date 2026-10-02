"""U — 세션 VWAP σ 밴드 평균회귀 (스캘핑, 15분봉 이하). 판정은 T 와 같은 사전 고정 + 홀드아웃.

가설. 짧은 시간축의 BTC 는 추세보다 되돌림이 조금 더 잦다 — 추세추종(SuperTrend)보다 평균회귀가
스캘핑 성격에 맞다. 세션(UTC 일) VWAP 에서 kσ 이상 벌어지면 반대로 들어가고 VWAP 로 돌아오면 나온다.

    진입  롱: VWAP_Z < −k   숏: VWAP_Z > +k
    청산  VWAP_Z 가 0 을 지나면(롱은 위로, 숏은 아래로 — 'crossOver OR crossUnder 0' 으로 표현:
          −k 아래서 들어간 롱은 0 을 위로 넘기 전엔 아래로 넘을 수 없다)
          + 선택: 퍼센트 손절 · 시간청산

정직한 사전 확률: **낮음.** P 가 잰 짧은 시간축 구조 효과는 1~9.6bp 인데 taker 왕복이 10bp 다.
그래서 --zero-fee 로 '수수료가 없으면 신호에 정보가 있나'를 함께 본다.

판정(T 와 동일, 결과 전 고정):
  ① 후보 = 구간1·2 모두 수익 > 0 + 각 구간 매칭 귀무의 best-of-N p95 초과 (N = 격자 크기)
  ② 후보 중 min(구간1, 구간2) 최대 하나 → ③ 홀드아웃(구간3) 1회: 수익 > 0 + 귀무 p95 초과
  ④ 후보 0 이면 그게 결론. 문턱을 낮추지 않는다.

    python3 -u -m research.exp_U_vwap_band
    python3 -u -m research.exp_U_vwap_band --zero-fee
    python3 -u -m research.exp_U_vwap_band --tfs 5m,15m [--zero-fee]
"""
from __future__ import annotations

import itertools
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import null_model as nm                           # noqa: E402
from research import lib                                       # noqa: E402
from research.exp_K_live_preset import hold_bars_of, mixed_null, one_way_fee  # noqa: E402
from research.exp_T_supertrend_search import _ym, segments     # noqa: E402

SYMBOL = "BTCUSDT"
ZERO_FEE = "--zero-fee" in sys.argv
TFS = ("3m", "5m", "15m")
if "--tfs" in sys.argv:                 # 예: --tfs 5m,15m (3m 은 부하가 크면 2시간 제한에 걸린다)
    TFS = tuple(sys.argv[sys.argv.index("--tfs") + 1].split(","))
KS = (1.5, 2.0, 2.5, 3.0)
STOPS = (None, 0.5, 1.0)              # 퍼센트 손절(가격 기준)
TIME_STOPS = (None, 24)               # 봉 수
SIDES = ("both", "long", "short")
GRID = list(itertools.product(TFS, KS, STOPS, TIME_STOPS, SIDES))

_W = {}
Z = {"indicator": "VWAP_Z"}


def build(tf, k, stop, tstop, side) -> dict:
    rules = []
    if side in ("both", "long"):
        rules.append({"side": "long", "when": {"left": Z, "cmp": "<", "right": -k}})
    if side in ("both", "short"):
        rules.append({"side": "short", "when": {"left": Z, "cmp": ">", "right": k}})
    ex = {"condition": {"op": "OR", "children": [
        {"cross": "crossOver", "left": Z, "right": 0},
        {"cross": "crossUnder", "left": Z, "right": 0}]}}
    if stop:
        ex["stopLoss"] = {"type": "percent", "value": stop}
    if tstop:
        ex["timeStop"] = {"maxBars": tstop}
    return {
        "schemaVersion": "1.0", "name": f"VWAPσ {tf} k{k}",
        "market": {"exchange": "binance-futures", "symbol": SYMBOL, "timeframe": tf, "direction": side},
        "entry": {"op": "OR", "children": [r["when"] for r in rules]},
        "entryRules": rules,
        "exit": ex,
        "sizing": {"leverage": 1, "marginMode": "isolated",
                   "size": {"type": "equityPercent", "value": 100.0}},
        "filter": {"cooldownBars": 1},
    }


def _label(c):
    tf, k, st, ts, side = c
    return f"{tf:>3} k={k} 손절{st or '-'} 시간{ts or '-'} {side}"


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


def _null(m, base, tf, n_combos):
    nl = sum(1 for t in m.trades if t.side == 1)
    hold = hold_bars_of(m, lib.TIMEFRAME_MINUTES[tf])
    dist = mixed_null(base, nl, m.num_trades - nl, hold, 1, 1.0, one_way_fee(m), 0)
    return nm.gate(m.total_return_pct, dist, n_combos=n_combos)


def main() -> int:
    segs = segments()
    workers = max(1, (os.cpu_count() or 2) - 1)
    fee = 0.0 if ZERO_FEE else None
    print(f"U · 세션 VWAP σ 밴드 평균회귀 · {SYMBOL} · TF {'/'.join(TFS)} · 격자 {len(GRID)} · "
          f"수수료 {'0 (상한)' if ZERO_FEE else '전부 taker'} · 워커 {workers}")
    for i, (a, b) in enumerate(segs):
        print(f"  구간{i+1}: {_ym(a)} ~ {_ym(b)}{'  (홀드아웃 — 선택 후 1회)' if i == 2 else ''}")

    res = {}
    jobs = [(c, i) for c in GRID for i in (0, 1)]
    with ProcessPoolExecutor(workers, initializer=_init, initargs=((0, 1), ZERO_FEE)) as ex:
        for c, i, m in ex.map(_run, jobs, chunksize=4):
            res[(c, i)] = m

    data = {i: lib.load(SYMBOL, start_ms=segs[i][0], end_ms=segs[i][1]) for i in (0, 1)}
    for tf in TFS:
        sub = [c for c in GRID if c[0] == tf]
        r0 = [res[(c, 0)].total_return_pct for c in sub]
        r1 = [res[(c, 1)].total_return_pct for c in sub]
        tr = [res[(c, 0)].num_trades for c in sub]
        print(f"  {tf:>3}: 중앙 {np.median(r0):+7.1f}% / {np.median(r1):+7.1f}%  최고 {max(r0):+7.1f}% / "
              f"{max(r1):+7.1f}%  두 구간 양수 {sum(a > 0 and b > 0 for a, b in zip(r0, r1))}/{len(sub)}  "
              f"거래 중앙 {int(np.median(tr))}")

    both_pos = [c for c in GRID if res[(c, 0)].total_return_pct > 0 and res[(c, 1)].total_return_pct > 0]
    print(f"\n  두 구간 모두 양수: {len(both_pos)}/{len(GRID)}")
    top = sorted(both_pos, key=lambda c: -min(res[(c, 0)].total_return_pct, res[(c, 1)].total_return_pct))
    passed = []
    if top:
        print(f"\n  {'조합':34} {'구간1':>9} {'거래':>6} {'구간2':>9} {'거래':>6}   best-of-{len(GRID)} p95")
    for n, c in enumerate(top):
        g = [_null(res[(c, i)], data[i][0], c[0], len(GRID)) for i in (0, 1)]
        ok = all(x["beatsBestOfN"] for x in g)
        if ok:
            passed.append(c)
        if n < 15 or ok:
            print(f"  {_label(c):34} {res[(c, 0)].total_return_pct:+8.1f}% {res[(c, 0)].num_trades:6d} "
                  f"{res[(c, 1)].total_return_pct:+8.1f}% {res[(c, 1)].num_trades:6d}   "
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
