"""E — SuperTrend 신호는 그대로, 필터만 새로. (사전 고정 판정 + 홀드아웃, evt 의 데이터·판정 재사용)

요청(2026-10-06): "SuperTrend 는 매수매도 신호로 계속 쓰고 싶다. 지금 필터(HawkEye+QQE) 말고 다른 필터."
HawkEye+QQE 는 K·T 에서, ADX 는 A2, 체결 쏠림은 C 에서 이미 기각. C 의 교훈은 '병목은 필터가 아니라
뒤집힘(휩쏘)'이라, 필터는 **가짜 뒤집힘을 거르는 다른 정보**여야 한다. 그래서 셋:

  상위TF   4h(또는 1h) SuperTrend(14,2.5) 방향과 같은 쪽만 — T 의 선택 통과 12개가 전부 4h 였다.
  변동성   ATR(14)/종가 가 직전 30일 평균의 v 배 이상일 때만 — 휩쏘는 저변동 횡보에서 난다.
  펀딩     직전 정산 펀딩 ≥ 0.03%(기본 0.01% 의 3배)면 롱 금지, < 0 이면 숏 금지 — 쏠린 쪽 회피.

신호·청산은 현행 그대로: SuperTrend(14,2.5) 뒤집힘 종가 진입 → 반대 뒤집힘 종가 청산.
뒤집힘이 번갈아 나오므로 거래 = 뒤집힘 사이 구간 하나. 필터는 **어느 구간을 탈지**만 고른다.
회계: 1배·자본 100%·복리·왕복 taker(net) 또는 0(gross). 펀딩은 전략·귀무 모두 뺀다(보유 수 시간, ~1bp).

귀무 둘 (둘 다 넘어야):
  시간 귀무  아무 시각 진입, 같은 횟수·평균 보유·롱숏 구성 (K·T 와 같은 근사 — engine/null_model)
  구간 귀무  **같은 SuperTrend 구간들 중 같은 개수를 무작위로** 고르기 — '필터가 무작위로 고른 것보다
            나은 구간을 골랐나'. 필터 자체의 값어치를 묻는 귀무다.

격자 = TF(5m·15m) × 상위TF(off·1h·4h) × 변동성(off·1.0·1.3) × 펀딩(off·on) × 방향(both·long) = 72.
판정은 T 와 동일: 구간1·2 양수 + best-of-72 p95(두 귀무) → min 최대 하나 → 홀드아웃 1회.

    python3 -u -m research.exp_E_st_filters
"""
from __future__ import annotations

import itertools
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                 # noqa: E402
from engine import indicators as ind                  # noqa: E402
from engine import null_model as nm                   # noqa: E402
from engine.candles import TIMEFRAME_MINUTES, resample  # noqa: E402
from research import evt                              # noqa: E402
from research.exp_T_supertrend_search import _ym, segments  # noqa: E402

ST = (14, 2.5)
TFS = ("5m", "15m")
HTFS = ("off", "1h", "4h")
VOLS = (None, 1.0, 1.3)
FUNDS = (False, True)
SIDES = ("both", "long")
GRID = list(itertools.product(TFS, HTFS, VOLS, FUNDS, SIDES))
FUND_HI = 0.0003
SAMPLES = 2000

_C: dict = {}


def legs(tf: str, seg: int):
    """SuperTrend 구간들: (진입 idx, 청산 idx, 방향) + 필터용 문맥. 캐시."""
    key = (tf, seg)
    if key in _C:
        return _C[key]
    b = evt.bars(tf, seg)
    _, d = ind.supertrend(b.high, b.low, b.close, *ST)
    flips = np.nonzero((d[1:] != d[:-1]) & ~np.isnan(d[1:]) & ~np.isnan(d[:-1]))[0] + 1
    ent, ext = flips[:-1], flips[1:]
    side = d[ent]
    end = b.open_time + b.timeframe_min * 60_000             # 진입 시각 = 봉 종가 시각
    # 상위TF 방향 — 진입 시각에 이미 닫힌 상위봉 기준(룩어헤드 없음)
    htf = {}
    for h in ("1h", "4h"):
        hb = resample(evt.base(seg), TIMEFRAME_MINUTES[h])
        _, hd = ind.supertrend(hb.high, hb.low, hb.close, *ST)
        k = np.searchsorted(hb.open_time + TIMEFRAME_MINUTES[h] * 60_000, end[ent], side="right") - 1
        htf[h] = np.where(k >= 0, np.nan_to_num(hd[np.clip(k, 0, None)]), 0.0)
    atr = ind.atr(b.high, b.low, b.close, 14) / b.close
    rel = atr / evt.past_mean(np.nan_to_num(atr), 30 * evt.day_bars_of(b))
    a, z = segments()[seg]
    fs = sorted(cs.funding_schedule("BTCUSDT", a - 86_400_000, z).items())
    ft = np.array([t for t, _ in fs], dtype=np.int64)
    fr = np.array([r for _, r in fs])
    j = np.searchsorted(ft, end[ent], side="right") - 1
    fund = np.where(j >= 0, fr[np.clip(j, 0, None)], 0.0)
    raw = b.close[ext] / b.close[ent] - 1.0
    _C[key] = dict(ent=ent, ext=ext, side=side, htf=htf, vol=rel[ent], fund=fund, raw=raw, b=b)
    return _C[key]


def pick(L, combo):
    _, htf, vol, fund, sd = combo
    keep = np.ones(len(L["ent"]), dtype=bool)
    if htf != "off":
        keep &= L["htf"][htf] == L["side"]
    if vol is not None:
        with np.errstate(invalid="ignore"):
            keep &= L["vol"] >= vol
    if fund:
        keep &= ~((L["side"] > 0) & (L["fund"] >= FUND_HI)) & ~((L["side"] < 0) & (L["fund"] < 0))
    if sd == "long":
        keep &= L["side"] > 0
    return keep


def ret(raw, side, fee):
    pt = np.clip(1.0 + side * raw - 2 * fee, 0.0, None)
    return float((np.prod(pt) - 1.0) * 100.0) if len(pt) else 0.0


def evaluate(combo, seg, fee):
    L = legs(combo[0], seg)
    k = pick(L, combo)
    r = ret(L["raw"][k], L["side"][k], fee)
    hold = (L["ext"][k] - L["ent"][k])
    return dict(ret=r, n=int(k.sum()), nl=int((L["side"][k] > 0).sum()),
                hold=int(round(hold.mean())) if k.any() else 1,
                bp=float((L["side"][k] * L["raw"][k]).mean() * 1e4) if k.any() else 0.0, keep=k)


def time_null(combo, seg, e, fee):
    out = None
    for n, s in ((e["nl"], "long"), (e["n"] - e["nl"], "short")):
        if n <= 0:
            continue
        d = 1.0 + nm.simulate(evt.base(seg), TIMEFRAME_MINUTES[combo[0]], n, e["hold"], s,
                              size_fraction=1.0, samples=SAMPLES, taker_fee=fee) / 100.0
        out = d if out is None else out * d
    return (out - 1.0) * 100.0 if out is not None else np.zeros(1)


def leg_null(combo, seg, e, fee, seed=0):
    """같은 SuperTrend 구간에서 같은 롱·숏 개수를 무작위로 고른다(방향 필터 'long' 이면 롱 구간에서만)."""
    L = legs(combo[0], seg)
    rng = np.random.default_rng(seed)
    out = np.empty(SAMPLES)
    il, is_ = np.nonzero(L["side"] > 0)[0], np.nonzero(L["side"] < 0)[0]
    for t in range(SAMPLES):
        pick_i = np.concatenate((rng.choice(il, e["nl"], replace=False),
                                 rng.choice(is_, e["n"] - e["nl"], replace=False)))
        out[t] = ret(L["raw"][pick_i], L["side"][pick_i], fee)
    return out


def judge(combo, seg, e, fee, n_combos):
    g1 = nm.gate(e["ret"], time_null(combo, seg, e, fee), n_combos=n_combos)
    g2 = nm.gate(e["ret"], leg_null(combo, seg, e, fee), n_combos=n_combos)
    ok = g1["beatsBestOfN"] and g2["beatsBestOfN"] and e["ret"] > 0 and e["n"] >= evt.MIN_TRADES
    return g1, g2, ok


def label(c):
    tf, htf, vol, fund, sd = c
    return f"{tf:>3} 상위:{htf:3} 변동성:{'-' if vol is None else vol:3} 펀딩:{'on' if fund else '- ':2} {sd}"


def main() -> int:
    segs = segments()
    print(f"E · SuperTrend{ST} 뒤집힘 + 새 필터(상위TF·변동성·펀딩) · BTCUSDT · 격자 {len(GRID)}")
    for i, (a, b) in enumerate(segs):
        print(f"  구간{i+1}: {_ym(a)} ~ {_ym(b)}{'  (홀드아웃 — 선택 후 1회)' if i == 2 else ''}")
    for stage, fee in (("gross(수수료 0)", 0.0), ("net(taker 왕복 10bp)", evt.TAKER)):
        print(f"\n── {stage} " + "─" * 50, flush=True)
        res = {(c, i): evaluate(c, i, fee) for c in GRID for i in (0, 1)}
        # 필터별 효과(조합 중앙값) — '켜면 나아지나'
        for name, pos, vals in (("상위TF", 1, HTFS), ("변동성", 2, VOLS), ("펀딩", 3, FUNDS), ("방향", 4, SIDES)):
            cells = []
            for v in vals:
                sub = [c for c in GRID if c[pos] == v]
                cells.append(f"{'-' if v is None else v}: {np.median([res[(c, 0)].get('ret') for c in sub]):+6.1f}%/"
                             f"{np.median([res[(c, 1)].get('ret') for c in sub]):+6.1f}% "
                             f"({np.median([res[(c, 0)]['bp'] for c in sub]):+.0f}/{np.median([res[(c, 1)]['bp'] for c in sub]):+.0f}bp)")
            print(f"  {name:5} " + " · ".join(cells))
        base = {tf: [res[((tf, 'off', None, False, 'both'), i)] for i in (0, 1)] for tf in TFS}
        for tf in TFS:
            print(f"  기준(필터 없음) {tf}: {base[tf][0]['ret']:+.1f}% / {base[tf][1]['ret']:+.1f}%  "
                  f"거래 {base[tf][0]['n']}/{base[tf][1]['n']}  거래당 {base[tf][0]['bp']:+.1f}/{base[tf][1]['bp']:+.1f}bp")
        cand = [c for c in GRID if all(res[(c, i)]["ret"] > 0 and res[(c, i)]["n"] >= evt.MIN_TRADES for i in (0, 1))]
        cand.sort(key=lambda c: -min(res[(c, 0)]["ret"], res[(c, 1)]["ret"]))
        print(f"  두 구간 양수: {len(cand)}/{len(GRID)}")
        passed = []
        if cand:
            print(f"\n  {'조합':40} {'구간1':>8} {'거래':>5} {'구간2':>8} {'거래':>5}   시간귀무 best-of-N p95 · 구간귀무")
        for c in cand:
            gs = [judge(c, i, res[(c, i)], fee, len(GRID)) for i in (0, 1)]
            ok = all(g[2] for g in gs)
            if ok:
                passed.append(c)
            a, b = res[(c, 0)], res[(c, 1)]
            print(f"  {label(c):40} {a['ret']:+7.1f}% {a['n']:5d} {b['ret']:+7.1f}% {b['n']:5d}   "
                  f"{gs[0][0]['bestOfNP95']:+7.1f} / {gs[1][0]['bestOfNP95']:+7.1f} · "
                  f"{gs[0][1]['bestOfNP95']:+7.1f} / {gs[1][1]['bestOfNP95']:+7.1f}  {'✅' if ok else '❌'}", flush=True)
        if not passed:
            print(f"\n  ★ {stage}: 사전 규칙 ①을 통과한 조합이 없다 → 홀드아웃을 열지 않는다.")
            continue
        pk = passed[0]
        e3 = evaluate(pk, 2, fee)
        g1, g2, ok = judge(pk, 2, e3, fee, 1)
        print(f"\n  ★ 선택: {label(pk)} → 홀드아웃 {e3['ret']:+.1f}% · 거래 {e3['n']} · 거래당 {e3['bp']:+.1f}bp")
        print(f"    시간귀무 중앙 {g1['nullMedian']:+.1f}% · p95 {g1['nullP95']:+.1f}% · 백분위 {g1['percentile']:.0f}"
              f" | 구간귀무 중앙 {g2['nullMedian']:+.1f}% · p95 {g2['nullP95']:+.1f}% · 백분위 {g2['percentile']:.0f}")
        print(f"    판정: {'✅ 통과' if ok else '❌ 홀드아웃 실패'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
