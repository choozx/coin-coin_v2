"""SR — 4h SuperTrend 방향 + 하위봉 RSI 눌림 (추세 방향으로만, 일시적으로 밀렸을 때 산다).

요청(2026-10-07): "RSI 와 SuperTrend 를 이용한 전략", "RSI 기준값 20 까지".
지금까지 기각된 ST 계열은 전부 **뒤집힘을 진입 신호**로 썼다(K·T·T4·E). 여기선 뒤집힘을 진입에 안 쓴다.

★ 같은 시간봉에선 이 조합이 성립하지 않는다(측정): 1h RSI 30 하향돌파 337번 중 그 봉 ST(14,2.5) 상승은 **0번**,
  RSI 20 은 모든 설정에서 0 — RSI 를 30 아래로 끌어내릴 만큼의 하락이면 같은 봉에서 ST 가 하락으로 뒤집힌다.
  그래서 추세는 **느린 4h ST** 로 읽고, 눌림은 **15m·1h RSI** 로 잡는다. (프리셋 스키마는 다른 시간봉 지표를 못
  쓰니 여기서 벡터로 구현한다.)

규칙:
  추세   4h SuperTrend 방향(진입 시각에 이미 닫힌 4h 봉 기준, 룩어헤드 없음)
  진입   추세 상승 + RSI(14) 가 θ 아래로 하향돌파 → 롱 / 추세 하락 + RSI 가 100−θ 위로 상향돌파 → 숏. 그 봉 종가.
  청산   rsi50: RSI 가 50 회복(롱 ≥50 / 숏 ≤50) · trend: 4h 추세가 뒤집히면 · time12: 12봉 보유
  회계   1배·자본 100%·복리·taker 5bp×2. 포지션 하나. 펀딩은 전략·귀무 모두 뺀다.

격자(결과 전 고정): 진입봉(15m·1h) × 4h ST(10,3·14,2.5·20,4) × θ(20·25·30·35·40) × 청산 3 × 방향(롱숏·롱만) = 180.
판정(결과 전 고정):
  주력(1h 진입 — 알트 데이터가 1h 라 다른 코인 검증이 된다)
    ① BTC 세 구간 모두 거래 ≥10 인 조합 중 min(구간 수익) 최대 하나
    ② 그 하나를 숫자 그대로 알트 55개(1h, 2021-01~) → 정밀 귀무 p95 초과 **≥ 7/55** 그리고 **알트 수익 중앙 > 0**
  참고(15m 진입 — 알트 1분봉이 없어 다른 코인 검증 불가): 같은 규칙으로 BTC 안에서만 최선 하나 + BTC 정밀 귀무.

    python3 -u -m research.exp_SR_st_rsi
"""
from __future__ import annotations

import itertools
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import indicators as ind                       # noqa: E402
from engine import null_model as nm                        # noqa: E402
from engine.candles import resample                        # noqa: E402
from research import evt                                    # noqa: E402
from research import lib                                   # noqa: E402
from research.exp_T4_params import alt_bases               # noqa: E402
from research.exp_T_supertrend_search import _ym, segments # noqa: E402

FEE = 0.0005
TFS = ("15m", "1h")
STS = ((10, 3.0), (14, 2.5), (20, 4.0))
THS = (20, 25, 30, 35, 40)
EXITS = ("rsi50", "trend", "time12")
SIDES = ("both", "long")
GRID = list(itertools.product(TFS, STS, THS, EXITS, SIDES))
MIN_TRADES = 10
NEED_BEATS = 7
TFMIN = {"15m": 15, "1h": 60}


class Tr:
    __slots__ = ("side", "entry_time", "exit_time", "leverage", "entry_price", "qty")

    def __init__(self, side, et, xt):
        self.side, self.entry_time, self.exit_time = side, et, xt
        self.leverage, self.entry_price, self.qty = 1, 1.0, 1.0


_CTX: dict = {}


def ctx(b, st):
    """진입봉 b 에 대해: RSI, 4h 추세(진입 시각 기준 이미 닫힌 4h 봉). 캐시."""
    key = (evt.pin(b), st)
    if key not in _CTX:
        h4 = resample(b, 240)
        _, d = ind.supertrend(h4.high, h4.low, h4.close, *st)
        end = b.open_time + b.timeframe_min * 60_000
        k = np.searchsorted(h4.open_time + 240 * 60_000, end, side="right") - 1
        trend = np.where(k >= 0, np.nan_to_num(d[np.clip(k, 0, None)]), 0.0)
        _CTX[key] = (ind.rsi(b.close, 14), trend)
    return _CTX[key]


def trades(b, combo):
    _, st, th, ex, side = combo
    rsi, trend = ctx(b, st)
    n = len(b.close)
    up = np.zeros(n, bool)
    dn = np.zeros(n, bool)
    with np.errstate(invalid="ignore"):
        up[1:] = (trend[1:] > 0) & (rsi[1:] < th) & (rsi[:-1] >= th)
        dn[1:] = (trend[1:] < 0) & (rsi[1:] > 100 - th) & (rsi[:-1] <= 100 - th)
    if side == "long":
        dn[:] = False
    out = []
    i = 0
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
        i = j                                   # 청산 봉에서 바로 다음 신호 가능
    return out


def evaluate(b, combo, fee=FEE):
    tr = trades(b, combo)
    if not tr:
        return 0.0, 0, []
    c = b.close
    pt = np.array([1.0 + s * (c[j] / c[i] - 1.0) - 2 * fee for i, j, s in tr])
    ret = float((np.prod(np.clip(pt, 0, None)) - 1.0) * 100.0)
    step = b.timeframe_min * 60_000
    objs = [Tr(s, int(b.open_time[i]), int(b.open_time[i]) + (j - i) * step) for i, j, s in tr]
    return ret, len(tr), objs


def judge(b, ret, objs, fee=FEE):
    dist = nm.simulate_trades(b, b.timeframe_min, objs, leverage=1, size_fraction=1.0, taker_fee=fee)
    return nm.gate(ret, dist)


def label(c):
    tf, st, th, ex, side = c
    return f"{tf:>3} 4hST{st} RSI {th}/{100 - th} 청산:{ex:6} {'롱숏' if side == 'both' else '롱만'}"


def main() -> int:
    segs = segments()
    print(f"SR · 4h SuperTrend 방향 + 하위봉 RSI 눌림 · 고르기 BTCUSDT 세 구간 · 검증 알트 55(1h 만) · 격자 {len(GRID)}")
    for i, (a, b) in enumerate(segs):
        print(f"  구간{i+1}: {_ym(a)} ~ {_ym(b)}")
    bars = {}
    for i, (a, b) in enumerate(segs):
        base = lib.load("BTCUSDT", start_ms=a, end_ms=b, with_funding=False)[0]
        for tf in TFS:
            bars[(tf, i)] = resample(base, TFMIN[tf])
    res = {(c, i): evaluate(bars[(c[0], i)], c)[:2] for c in GRID for i in (0, 1, 2)}

    print("\n  θ 별 조합 중앙 (구간1 / 2 / 3, 거래 중앙)")
    for tf in TFS:
        for th in THS:
            sub = [c for c in GRID if c[0] == tf and c[2] == th]
            print(f"   {tf:>3} RSI {th:2d}: " + " / ".join(f"{np.median([res[(c, i)][0] for c in sub]):+7.1f}%" for i in (0, 1, 2))
                  + f"   거래 {int(np.median([res[(c, 0)][1] for c in sub]))}")

    for tf, main_ in (("1h", True), ("15m", False)):
        ok = [c for c in GRID if c[0] == tf and all(res[(c, i)][1] >= MIN_TRADES for i in (0, 1, 2))]
        ok.sort(key=lambda c: -min(res[(c, i)][0] for i in (0, 1, 2)))
        allpos = sum(all(res[(c, i)][0] > 0 for i in (0, 1, 2)) for c in ok)
        print(f"\n■ {tf} 진입 {'[주력 — 알트 검증]' if main_ else '[참고 — BTC 만]'} · 세 구간 모두 양수 {allpos}/{len([c for c in GRID if c[0] == tf])}. 상위 5:")
        for c in ok[:5]:
            print(f"  {label(c):44} " + " / ".join(f"{res[(c, i)][0]:+7.1f}% ({res[(c, i)][1]:3d})" for i in (0, 1, 2)))
        if not ok:
            print("  거래 조건을 채운 조합 없음")
            continue
        pick = ok[0]
        print(f"  ★ 선택: {label(pick)}")
        g = []
        for i in (0, 1, 2):
            r, n, objs = evaluate(bars[(tf, i)], pick)
            g.append(judge(bars[(tf, i)], r, objs))
        print("  BTC 정밀 귀무 백분위: " + " / ".join(f"{x['percentile']:.0f}" for x in g)
              + "  (p95: " + " / ".join(f"{x['nullP95']:+.0f}%" for x in g) + ")")
        if not main_:
            print("  ※ 15m 은 다른 코인 1분봉이 없어 자산 축 검증을 못 한다 — 판정 아님, 참고.")
            continue
        alts = alt_bases()               # 1h 를 '마지막 1분'으로 옮긴 1분 라벨 → 여기선 시각만 되돌려 1h 로 쓴다
        rows = []
        for s, base, _ in alts:
            b1 = resample(base.__class__(base.open_time - 59 * 60_000, base.open, base.high, base.low, base.close,
                                         base.volume, 60, taker_buy=base.taker_buy), 60)
            r, n, objs = evaluate(b1, pick)
            gg = judge(b1, r, objs) if n else None
            rows.append((s, r, n, gg["percentile"] if gg else 0.0, bool(gg and gg["beatsNull"])))
        beats = sum(x[4] for x in rows)
        rets = np.array([x[1] for x in rows])
        print(f"\n  알트 {len(rows)}개(숫자 그대로): 수익 양수 {int((rets > 0).sum())}/{len(rows)} · 수익 중앙 {np.median(rets):+.1f}% · "
              f"정밀 귀무 p95 초과 {beats}/{len(rows)} (문턱 {NEED_BEATS}) · 백분위 중앙 {np.median([x[3] for x in rows]):.0f}")
        for s, r, n, p, bt in sorted(rows, key=lambda x: -x[1])[:5]:
            print(f"    상위 {s:12} {r:+8.1f}%  거래 {n:3d}  백분위 {p:3.0f}{' ✅' if bt else ''}")
        passed = beats >= NEED_BEATS and np.median(rets) > 0
        print(f"  판정: {'✅ 통과 — 다른 코인에서도 돈을 벌고 랜덤보다 낫다' if passed else '❌ 실패'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
