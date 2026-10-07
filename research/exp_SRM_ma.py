"""SRM — SR(4h SuperTrend 추세 + 하위봉 RSI 눌림)에 이동평균 필터를 더하면 나아지나.

요청(2026-10-07): "여기에 필터로 이동평균선도 포함하면?"
사전 예상: 낮음. 이평은 4h ST 와 같은 '가격 추세 방향' 정보라 겹친다(E: 상위TF 추세 필터가 거래당 bp 를 못 올림,
V: SMA200 방향+SMA22 기각). 그래도 1분짜리라 같은 판정으로 확인한다.

필터(결과 전 고정): 없음 · 1h SMA200(≈8일) · 4h SMA200(≈33일) · 1d SMA200(고전 '강세장 필터').
  롱은 직전에 닫힌 그 봉 종가 > SMA, 숏은 < SMA 일 때만 진입(SMA 가 아직 없으면 진입 안 함).
격자 = SR 180 × 필터 4 = 720. 판정은 SR 과 동일(1h 진입 주력: BTC 세 구간 min 최대 하나 → 알트 55 숫자 그대로,
정밀 귀무 p95 초과 ≥7/55 그리고 알트 수익 중앙 > 0). 필터별 '거래당 bp 중앙(켬 vs 끔)'도 따로 찍는다.

    python3 -u -m research.exp_SRM_ma
"""
from __future__ import annotations

import itertools
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine.candles import resample                          # noqa: E402
from research import evt                                    # noqa: E402
from research import lib                                     # noqa: E402
from research import exp_SR_st_rsi as SR                     # noqa: E402
from research.exp_T4_params import alt_bases                 # noqa: E402
from research.exp_T_supertrend_search import _ym, segments   # noqa: E402

MAS = ("none", "1h", "4h", "1d")
GRID = [c + (ma,) for c in SR.GRID for ma in MAS]
MA_MIN = {"1h": 60, "4h": 240, "1d": 1440}
_MA: dict = {}


def ma_side(b, ma):
    """봉마다 +1(종가 > SMA200) / −1(<) / 0(SMA 없음). 진입 시각에 이미 닫힌 MA 봉 기준."""
    key = (evt.pin(b), ma)
    if key not in _MA:
        if ma == "none":
            _MA[key] = None
        else:
            hb = resample(b, MA_MIN[ma]) if MA_MIN[ma] != b.timeframe_min else b
            c = hb.close
            sma = np.full(len(c), np.nan)
            if len(c) >= 200:
                cs = np.cumsum(np.concatenate(([0.0], c)))
                sma[199:] = (cs[200:] - cs[:-200]) / 200
            sgn = np.where(np.isnan(sma), 0.0, np.sign(c - sma))
            end = b.open_time + b.timeframe_min * 60_000
            k = np.searchsorted(hb.open_time + MA_MIN[ma] * 60_000, end, side="right") - 1
            _MA[key] = np.where(k >= 0, sgn[np.clip(k, 0, None)], 0.0)
    return _MA[key]


def trades(b, combo):
    *base, ma = combo
    _, st, th, ex, side = base
    rsi, trend = SR.ctx(b, st)
    n = len(b.close)
    up = np.zeros(n, bool)
    dn = np.zeros(n, bool)
    with np.errstate(invalid="ignore"):
        up[1:] = (trend[1:] > 0) & (rsi[1:] < th) & (rsi[:-1] >= th)
        dn[1:] = (trend[1:] < 0) & (rsi[1:] > 100 - th) & (rsi[:-1] <= 100 - th)
    m = ma_side(b, ma)
    if m is not None:
        up &= m > 0
        dn &= m < 0
    if side == "long":
        dn[:] = False
    out, i = [], 0
    while i < n - 1:
        if not (up[i] or dn[i]):
            i += 1
            continue
        s = 1 if up[i] else -1
        j = i + 1
        if ex == "time12":
            j = min(n - 1, i + 12)
        else:
            while j < n - 1:
                if ex == "rsi50" and ((s > 0 and rsi[j] >= 50) or (s < 0 and rsi[j] <= 50)):
                    break
                if ex == "trend" and trend[j] != trend[i]:
                    break
                j += 1
        out.append((i, j, s))
        i = j
    return out


def evaluate(b, combo, fee=SR.FEE):
    tr = trades(b, combo)
    if not tr:
        return 0.0, 0, [], 0.0
    c = b.close
    raw = np.array([s * (c[j] / c[i] - 1.0) for i, j, s in tr])
    ret = float((np.prod(np.clip(1.0 + raw - 2 * fee, 0, None)) - 1.0) * 100.0)
    step = b.timeframe_min * 60_000
    objs = [SR.Tr(s, int(b.open_time[i]), int(b.open_time[i]) + (j - i) * step) for i, j, s in tr]
    return ret, len(tr), objs, float(raw.mean() * 1e4)


def label(c):
    *base, ma = c
    return SR.label(tuple(base)) + f" 이평:{ma}"


def main() -> int:
    segs = segments()
    print(f"SRM · SR + 이동평균(SMA200) 필터 · 격자 {len(GRID)}")
    bars = {}
    for i, (a, b) in enumerate(segs):
        base = lib.load("BTCUSDT", start_ms=a, end_ms=b, with_funding=False)[0]
        for tf in SR.TFS:
            bars[(tf, i)] = resample(base, SR.TFMIN[tf])
    res = {}
    for c in GRID:
        for i in (0, 1, 2):
            r, n, _, bp = evaluate(bars[(c[0], i)], c)
            res[(c, i)] = (r, n, bp)

    print("\n  필터별 (조합 중앙) 수익 구간1/2/3 · 거래당 gross bp · 거래 수")
    for tf in SR.TFS:
        for ma in MAS:
            sub = [c for c in GRID if c[0] == tf and c[-1] == ma]
            rr = " / ".join(f"{np.median([res[(c, i)][0] for c in sub]):+6.1f}%" for i in (0, 1, 2))
            bp = np.median([res[(c, i)][2] for c in sub for i in (0, 1, 2) if res[(c, i)][1]])
            nn = int(np.median([res[(c, 0)][1] for c in sub]))
            print(f"   {tf:>3} 이평 {ma:4}: {rr}   거래당 {bp:+6.1f}bp   거래 {nn}")

    for tf, main_ in (("1h", True), ("15m", False)):
        ok = [c for c in GRID if c[0] == tf and all(res[(c, i)][1] >= SR.MIN_TRADES for i in (0, 1, 2))]
        ok.sort(key=lambda c: -min(res[(c, i)][0] for i in (0, 1, 2)))
        print(f"\n■ {tf} 진입 {'[주력 — 알트 검증]' if main_ else '[참고 — BTC 만]'} 상위 5:")
        for c in ok[:5]:
            print(f"  {label(c):54} " + " / ".join(f"{res[(c, i)][0]:+7.1f}% ({res[(c, i)][1]:3d})" for i in (0, 1, 2)))
        pick = ok[0]
        print(f"  ★ 선택: {label(pick)}")
        g = []
        for i in (0, 1, 2):
            r, n, objs, _ = evaluate(bars[(tf, i)], pick)
            g.append(SR.judge(bars[(tf, i)], r, objs))
        print("  BTC 정밀 귀무 백분위: " + " / ".join(f"{x['percentile']:.0f}" for x in g))
        if not main_:
            print("  ※ 15m 은 다른 코인 검증 불가 — 참고.")
            continue
        rows = []
        for s, base, _ in alt_bases():
            b1 = base.__class__(base.open_time - 59 * 60_000, base.open, base.high, base.low, base.close,
                                base.volume, 60, taker_buy=base.taker_buy)
            r, n, objs, _ = evaluate(b1, pick)
            gg = SR.judge(b1, r, objs) if n else None
            rows.append((s, r, n, gg["percentile"] if gg else 0.0, bool(gg and gg["beatsNull"])))
        beats = sum(x[4] for x in rows)
        rets = np.array([x[1] for x in rows])
        print(f"\n  알트 {len(rows)}개: 수익 양수 {int((rets > 0).sum())}/{len(rows)} · 수익 중앙 {np.median(rets):+.1f}% · "
              f"정밀 귀무 p95 초과 {beats}/{len(rows)} (문턱 {SR.NEED_BEATS}) · 백분위 중앙 {np.median([x[3] for x in rows]):.0f}")
        passed = beats >= SR.NEED_BEATS and np.median(rets) > 0
        print(f"  판정: {'✅ 통과' if passed else '❌ 실패'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
