"""T — SuperTrend 로 BTCUSDC 에서 낼 수 있는 최선을 찾는다 (사전 고정 판정 + 홀드아웃).

요청: "지금 쓰는 SuperTrend 로 BTCUSDC 최적 수익률". 이건 이 저장소가 가장 많이 속은 형태다 —
조합 수백 개 중 1등은 엣지가 없어도 거의 항상 좋아 보인다(5a94ab0: '+3.03% 최고 조합'이 우연의
1/8). 그래서 **결과를 보기 전에** 규칙을 여기 고정한다:

  선택 구간 = BTCUSDT 독립 3구간 중 앞 두 개(2019-09~2022-01, 2022-01~2024-05)
  홀드아웃  = 세 번째(2024-05~2026-08) — 선택이 끝날 때까지 **아무 조합도 돌리지 않는다**

  ① 후보: 두 선택 구간 모두 수익 > 0 이고, 각 구간에서 매칭 귀무의 **best-of-N p95**(N=격자 크기)
     를 넘는다. 450 개 중 1등을 고르는 일이므로 문턱도 '우연 450 개 중 1등'이다.
  ② 후보 중 min(구간1, 구간2) 수익이 가장 큰 조합 하나를 고른다(한 구간 대박 말고 둘 다 버틴 것).
  ③ 그 하나만 홀드아웃에서 **한 번** 돈다 — 수익 > 0 이고 귀무 p95(N=1) 를 넘어야 통과.
  ④ 후보가 0 이면 그게 결론이다. 문턱을 낮추지 않는다.

가격은 BTCUSDT(6.9년). BTCUSDC 캐시는 2025-09 부터라 구간을 못 나눈다. 수수료는 엔진 기본
(maker 체결률 0 = 전부 taker) — 테스트넷 실측 maker 4~10% 에 가장 가깝다.
레버리지 1·명목 100% 로 잰다. 레버리지는 엣지를 만들지 않고 결과와 파산 확률만 키운다.

    python3 -u -m research.exp_T_supertrend_search
    python3 -u -m research.exp_T_supertrend_search --scalp            # 3m·5m·15m 만(스캘핑)
    python3 -u -m research.exp_T_supertrend_search --scalp --zero-fee # 수수료 0 = 신호 자체에 정보가 있나

--scalp: 사용자 제약 "스캘핑이니 15분봉 이하". 1m 은 한 번에 ~190초·구간1 에서 2.4만 거래라 뺐다.
--zero-fee: maker 0%(BTCUSDC) 로 전부 체결된다는 상한. 귀무에도 같은 0 이 들어간다(F 의 교훈).
"""
from __future__ import annotations

import copy
import datetime as dt
import itertools
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                         # noqa: E402
from engine import null_model as nm                           # noqa: E402
from research import lib                                       # noqa: E402
from research.exp_K_live_preset import (hold_bars_of, live_preset,  # noqa: E402
                                         mixed_null, one_way_fee)

SYMBOL = "BTCUSDT"
SCALP = "--scalp" in sys.argv
ZERO_FEE = "--zero-fee" in sys.argv
TFS = ("3m", "5m", "15m") if SCALP else ("15m", "1h", "4h")
PERIODS = (7, 10, 14, 20, 30)
MULTS = (1.5, 2.0, 2.5, 3.0, 4.0)
FILTERS = (True, False)                 # 현행 HawkEye+QQE 필터 유지 / SuperTrend 단독
SIDES = ("both", "long", "short")
GRID = list(itertools.product(TFS, PERIODS, MULTS, FILTERS, SIDES))
NULL_SAMPLES = 2000

_W = {}


def segments():
    st = cs.stats(SYMBOL)
    lo, hi = st["min"], st["max"]
    span = (hi - lo) // 3
    return [(lo + i * span, lo + (i + 1) * span) for i in range(3)]


def build(tf, period, mult, filt, side) -> dict:
    d = copy.deepcopy(live_preset())
    d["market"]["timeframe"] = tf
    d["market"]["direction"] = side
    d["sizing"] = {"leverage": 1, "marginMode": "isolated",
                   "size": {"type": "equityPercent", "value": 100.0}}
    rules = []
    for r in d["entryRules"]:
        if side != "both" and r["side"] != side:
            continue
        kids = []
        for c in r["when"]["children"]:
            ind = c.get("left", {}).get("indicator")
            if ind == "SUPERTREND_DIR":
                c["left"]["period"] = period
                c["left"]["params"]["multiplier"] = mult
                kids.append(c)
            elif filt:
                kids.append(c)
        rules.append({"side": r["side"], "when": {"op": "AND", "children": kids}})
    d["entryRules"] = rules
    d["entry"] = {"op": "OR", "children": [r["when"] for r in rules]}
    d["exit"]["supertrendExit"].update({"period": period, "multiplier": mult})
    return d


def _init(seg_idx, zero_fee=False):
    _W["fee"] = 0.0 if zero_fee else None
    segs = segments()
    _W["data"] = {i: lib.load(SYMBOL, start_ms=segs[i][0], end_ms=segs[i][1]) for i in seg_idx}


def _run(args):
    combo, i = args
    base, fs = _W["data"][i]
    f = _W.get("fee")
    m = lib.backtest(base, build(*combo), SYMBOL, funding_schedule=fs, maker_fee=f, taker_fee=f)
    return combo, i, m


def _null(m, base, tf, n_combos):
    nl = sum(1 for t in m.trades if t.side == 1)
    hold = hold_bars_of(m, lib.TIMEFRAME_MINUTES[tf])
    dist = mixed_null(base, nl, m.num_trades - nl, hold, 1, 1.0, one_way_fee(m), 0)
    return nm.gate(m.total_return_pct, dist, n_combos=n_combos)


def _label(c):
    tf, p, mu, f, s = c
    return f"{tf:>3} ST({p},{mu}) {'필터' if f else '단독'} {s}"


def _ym(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m")


def main() -> int:
    segs = segments()
    workers = max(1, (os.cpu_count() or 2) - 1)
    fee = 0.0 if ZERO_FEE else None
    print(f"T · SuperTrend 탐색 · {SYMBOL} · TF {'/'.join(TFS)} · 격자 {len(GRID)} · "
          f"수수료 {'0 (상한)' if ZERO_FEE else '전부 taker'} · 워커 {workers}")
    for i, (a, b) in enumerate(segs):
        print(f"  구간{i+1}: {_ym(a)} ~ {_ym(b)}{'  (홀드아웃 — 선택 후 1회)' if i == 2 else ''}")

    # ── 선택: 구간 1·2 ──
    res = {}
    jobs = [(c, i) for c in GRID for i in (0, 1)]
    with ProcessPoolExecutor(workers, initializer=_init, initargs=((0, 1), ZERO_FEE)) as ex:
        for k, (c, i, m) in enumerate(ex.map(_run, jobs, chunksize=4), 1):
            res[(c, i)] = m
            if k % 100 == 0:
                print(f"  … {k}/{len(jobs)}", flush=True)

    data = {i: lib.load(SYMBOL, start_ms=segs[i][0], end_ms=segs[i][1]) for i in (0, 1)}
    for i in (0, 1):
        base = data[i][0]
        bh = (base.close[-1] / base.close[0] - 1) * 100
        rets = [res[(c, i)].total_return_pct for c in GRID]
        print(f"\n  구간{i+1} 분포: BTC 보유 {bh:+.0f}% · 조합 중앙 {np.median(rets):+.1f}% · "
              f"양수 {sum(r > 0 for r in rets)}/{len(rets)} · 최고 {max(rets):+.1f}%")

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
    print(f"\n  {'조합':32} {'구간1':>9} {'거래':>5} {'구간2':>9} {'거래':>5}   귀무 best-of-{len(GRID)} p95 (구간1 / 구간2)")
    passed = []
    for c in top:
        g = [_null(res[(c, i)], data[i][0], c[0], len(GRID)) for i in (0, 1)]
        ok = all(x["beatsBestOfN"] for x in g)
        if ok:
            passed.append(c)
        if top.index(c) < 15 or ok:
            print(f"  {_label(c):32} {res[(c, 0)].total_return_pct:+8.1f}% {res[(c, 0)].num_trades:5d} "
                  f"{res[(c, 1)].total_return_pct:+8.1f}% {res[(c, 1)].num_trades:5d}   "
                  f"{g[0]['bestOfNP95']:+8.1f} / {g[1]['bestOfNP95']:+8.1f}  {'✅' if ok else '❌'}", flush=True)

    # 현행 라이브 설정의 자리
    cur = ("15m", 14, 2.5, True, "both")
    if (cur, 0) in res:
        print(f"\n  현행(라이브) {_label(cur)}: 구간1 {res[(cur, 0)].total_return_pct:+.1f}% · "
              f"구간2 {res[(cur, 1)].total_return_pct:+.1f}%  (레버리지 1 기준)")

    if not passed:
        print("\n  ★ 결론: 사전 규칙 ①을 통과한 조합이 없다 → 홀드아웃을 열지 않는다.")
        return 0

    # ── 홀드아웃: 고른 하나만, 한 번 ──
    pick = passed[0]
    print(f"\n  ★ 선택: {_label(pick)} → 홀드아웃 1회")
    base3, fs3 = lib.load(SYMBOL, start_ms=segs[2][0], end_ms=segs[2][1])
    m3 = lib.backtest(base3, build(*pick), SYMBOL, funding_schedule=fs3, maker_fee=fee, taker_fee=fee)
    g3 = _null(m3, base3, pick[0], 1)
    bh3 = (base3.close[-1] / base3.close[0] - 1) * 100
    lib.show("  홀드아웃", m3)
    print(f"  BTC 보유 {bh3:+.0f}% · 귀무 중앙 {g3['nullMedian']:+.1f}% · p95 {g3['nullP95']:+.1f}% · "
          f"백분위 {g3['percentile']:.0f}")
    ok3 = g3["beatsNull"] and m3.total_return_pct > 0
    print(f"  판정: {'✅ 통과' if ok3 else '❌ 홀드아웃 실패'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
