"""FT — 펀딩비를 시간축 역추세 신호로: 쏠림이 극단이면 반대로 (BACKLOG D 의 마지막 미시험 갈래).

요청(2026-10-07): "랜덤 진입보다 좋은 것을 네가 찾아봐."
지금까지 기각된 건 거의 다 **가격에서 만든 신호**였다. 사전 판정을 통과한 M 은 **포지션 쏠림(펀딩비)** 을 썼다(횡단면).
같은 정보를 **시간축**으로: 한 코인의 펀딩이 자기 과거 대비 극단적으로 높으면(롱 과밀) 이후 며칠 약하고, 낮으면 강하다.

규칙(결과 전 고정):
  하루 펀딩 합(그날 00:00 UTC 까지 24h 정산 합 — 1h·4h·8h 주기 차이를 하루로 맞춤)의 **직전 90일 내 백분위** p
  p ≤ q → 롱 · p ≥ 1−q → 숏(롱만이면 쉼). 00:00 UTC 일봉 종가에 진입, H 일 보유 후 청산. 포지션 하나(겹치면 버림).
  격자 q(10·20%) × 방향(롱숏·롱만) × H(1·3·7일) = 12. 1배·자본 100%·taker 5bp·보유 중 실펀딩(롱은 양 요율을 낸다).
판정(결과 전 고정):
  ① BTC 세 구간 모두 거래 ≥10 인 조합 중 min(구간 수익) 최대 하나
  ② 그 하나를 숫자 그대로 알트 55개(2021-01~, 펀딩 실데이터) → 정밀 귀무 p95 초과 **≥ 7/55** 그리고 **수익 중앙 > 0**
  참고(판정 외): 같은 규칙의 '순방향'(쏠린 쪽을 따라감) — 방향이 맞는지 확인용.

    python3 -u -m research.exp_FT_funding_timing
"""
from __future__ import annotations

import itertools
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                        # noqa: E402
from engine import null_model as nm                          # noqa: E402
from engine.candles import resample                          # noqa: E402
from research import lib                                     # noqa: E402
from research.exp_SR_st_rsi import Tr                        # noqa: E402
from research.exp_T4_params import alt_bases                 # noqa: E402
from research.exp_T_supertrend_search import _ym, segments   # noqa: E402

FEE = 0.0005
DAY = 86_400_000
QS = (0.10, 0.20)
SIDES = ("both", "long")
HOLDS = (1, 3, 7)
GRID = list(itertools.product(QS, SIDES, HOLDS))
LOOK = 90
MIN_TRADES = 10
NEED_BEATS = 7


def daily(sym, b1d):
    """일봉마다: 그 봉 종가 시각까지 24h 펀딩 합(신호) · 그 봉 종가→다음 종가 사이 펀딩 합(보유 비용)."""
    end = b1d.open_time + DAY
    try:
        fs = cs.funding_schedule(sym, int(b1d.open_time[0]) - DAY, int(end[-1]) + 8 * DAY)
    except Exception:
        fs = {}
    if not fs:
        return None, None
    ft = np.array(sorted(fs), dtype=np.int64)
    fr = np.array([fs[t] for t in ft])
    cum = np.concatenate(([0.0], np.cumsum(fr)))
    upto = np.searchsorted(ft, end, side="right")
    frm = np.searchsorted(ft, end - DAY, side="right")
    sig = cum[upto] - cum[frm]                         # (종가−24h, 종가] 정산 합
    nxt = np.searchsorted(ft, end + DAY, side="right")
    cost = cum[nxt] - cum[upto]                        # (종가, 다음 종가] — 보유 중 낼 펀딩
    sig[upto == frm] = np.nan                          # 정산 데이터 없는 날
    return sig, cost


def pctl(sig):
    """직전 LOOK 일(당일 포함) 안에서 당일 값의 백분위. 앞 LOOK 일은 NaN."""
    p = np.full(len(sig), np.nan)
    for i in range(LOOK, len(sig)):
        w = sig[i - LOOK + 1:i + 1]
        w = w[~np.isnan(w)]
        if len(w) >= LOOK // 2 and not np.isnan(sig[i]):
            p[i] = (w < sig[i]).mean()
    return p


def run(b1d, p, cost, combo, mirror=False):
    q, side, h = combo
    c = b1d.close
    n = len(c)
    out, i = [], 0
    while i < n - h:
        s = 0
        if not np.isnan(p[i]):
            if p[i] <= q:
                s = 1
            elif p[i] >= 1 - q and side == "both":
                s = -1
        if mirror:
            s = -s if side == "both" else (1 if (not np.isnan(p[i]) and p[i] >= 1 - q) else 0)
        if s == 0:
            i += 1
            continue
        j = i + h
        fund = float(np.nansum(cost[i:j]))
        out.append((i, j, s, s * (c[j] / c[i] - 1.0) - 2 * FEE - s * fund))
        i = j
    if not out:
        return 0.0, 0, []
    ret = float((np.prod(np.clip([1.0 + x[3] for x in out], 0, None)) - 1.0) * 100.0)
    objs = [Tr(s, int(b1d.open_time[i]), int(b1d.open_time[i]) + (j - i) * DAY) for i, j, s, _ in out]
    return ret, len(out), objs


def judge(b1d, ret, objs):
    dist = nm.simulate_trades(b1d, 1440, objs, leverage=1, size_fraction=1.0, taker_fee=FEE)
    return nm.gate(ret, dist)


def label(c, mirror=False):
    q, side, h = c
    return f"{'순방향' if mirror else '역추세'} q{int(q * 100)}% {'롱숏' if side == 'both' else '롱만'} 보유 {h}일"


def main() -> int:
    segs = segments()
    print(f"FT · 펀딩 쏠림 역추세(시간축) · 고르기 BTCUSDT 세 구간 · 검증 알트 55 · 격자 {len(GRID)}")
    st = cs.stats("BTCUSDT")
    base = lib.load("BTCUSDT", start_ms=st["min"], end_ms=st["max"], with_funding=False)[0]
    b1d = resample(base, 1440)
    sig, cost = daily("BTCUSDT", b1d)
    p = pctl(sig)
    cuts = [int(np.searchsorted(b1d.open_time, a)) for a, _ in segs] + [len(b1d.close)]
    res = {}
    for c in GRID:
        for k in (0, 1, 2):
            sub = b1d.__class__(b1d.open_time[cuts[k]:cuts[k + 1]], b1d.open[cuts[k]:cuts[k + 1]],
                                b1d.high[cuts[k]:cuts[k + 1]], b1d.low[cuts[k]:cuts[k + 1]],
                                b1d.close[cuts[k]:cuts[k + 1]], b1d.volume[cuts[k]:cuts[k + 1]], 1440)
            r, n, objs = run(sub, p[cuts[k]:cuts[k + 1]], cost[cuts[k]:cuts[k + 1]], c)
            res[(c, k)] = (r, n, objs, sub)
    print(f"\n  {'조합':26} {'구간1':>16} {'구간2':>16} {'구간3':>16}   (거래 수 · 랜덤 백분위)")
    for c in GRID:
        cells = []
        for k in (0, 1, 2):
            r, n, objs, sub = res[(c, k)]
            g = judge(sub, r, objs) if n else None
            cells.append(f"{r:+7.1f}% ({n:3d}·{g['percentile'] if g else 0:3.0f})")
        print(f"  {label(c):26} " + " ".join(f"{x:>16}" for x in cells))
    ok = [c for c in GRID if all(res[(c, k)][1] >= MIN_TRADES for k in (0, 1, 2))]
    ok.sort(key=lambda c: -min(res[(c, k)][0] for k in (0, 1, 2)))
    if not ok:
        print("\n  거래 조건을 채운 조합 없음 → 결론: 신호가 너무 드물다")
        return 0
    pick = ok[0]
    print(f"\n  ★ 선택: {label(pick)}  (BTC " + " / ".join(f"{res[(pick, k)][0]:+.1f}%" for k in (0, 1, 2)) + ")")

    rows, rows_m = [], []
    for s, b, _ in alt_bases():
        b1h = b.__class__(b.open_time - 59 * 60_000, b.open, b.high, b.low, b.close, b.volume, 60, taker_buy=b.taker_buy)
        a1d = resample(b1h, 1440)
        sg, cst = daily(s, a1d)
        if sg is None:
            continue
        pa = pctl(sg)
        for mirror, store in ((False, rows), (True, rows_m)):
            r, n, objs = run(a1d, pa, cst, pick, mirror=mirror)
            g = judge(a1d, r, objs) if n else None
            store.append((s, r, n, g["percentile"] if g else 0.0, bool(g and g["beatsNull"])))
    for name, rr in ((label(pick), rows), (label(pick, True) + " [참고]", rows_m)):
        rets = np.array([x[1] for x in rr])
        beats = sum(x[4] for x in rr)
        print(f"\n  알트 {len(rr)}개 · {name}: 수익 양수 {int((rets > 0).sum())}/{len(rr)} · 수익 중앙 {np.median(rets):+.1f}% · "
              f"정밀 귀무 p95 초과 {beats}/{len(rr)} · 백분위 중앙 {np.median([x[3] for x in rr]):.0f} · 거래 중앙 {int(np.median([x[2] for x in rr]))}")
    rets = np.array([x[1] for x in rows])
    passed = sum(x[4] for x in rows) >= NEED_BEATS and np.median(rets) > 0
    print(f"\n  판정: {'✅ 통과' if passed else '❌ 실패'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
