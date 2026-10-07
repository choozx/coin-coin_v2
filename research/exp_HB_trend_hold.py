"""HB — '덜 잃는 보유': 추세가 좋을 때만 1배 롱, 아니면 현금. 목적은 더 벌기가 아니라 낙폭 줄이기.

요청(2026-10-07, 사용자가 B 선택): 엣지 전략이 없으니 '보유하되 하락장을 피하는' 규칙을 본다.
반복 관찰(T4·SR·SRM): 4h 추세 계열은 알트에서 늘 랜덤보다 약간 낫고, 하락장 알트에서 보유보다 덜 잃었다.

규칙(결과 전 고정, 셋):
  B1  일봉 종가 > SMA200(1d)        — 고전 '강세장 필터'
  B2  4h SuperTrend(14,2.5) 상승     — 현행 프리셋의 ST 설정
  B3  B1 그리고 B2
  판정은 각 봉 종가(이미 닫힌 1d·4h 봉 기준) → 다음 4h 동안 보유 여부. 1배·자본 100%.
비용: 전환마다 taker 5bp. **선물 보유 = 펀딩 지불**(롱이면 양(+) 요율을 낸다) — 실데이터. 현물이었다면(펀딩 없음)도 같이 찍는다.
기준선: 같은 상품 그냥 보유(선물 보유는 펀딩 포함, 현물 보유는 없음).

판정(결과 전 고정, 셋 다):
  ① 최대 낙폭(MDD)이 BTC 세 구간 모두 보유보다 작고, 알트 55 중 ≥ 80% 에서 보유보다 작다
  ② 위험 대비 수익(연수익 ÷ MDD, Calmar)이 BTC 전체 기간에서 보유보다 크고, 알트 중 > 50% 에서 보유보다 크다
  ③ BTC 전체 Calmar 가 **같은 보유 비율·전환 횟수의 랜덤 보유**(보유 여부 시계열을 통째로 원형 이동 1,000회)의
     95 백분위 이상 — 현금 비중을 늘리면 낙폭이 주는 건 당연하니, 규칙이 '언제 빠질지'를 아는지 묻는다.

    python3 -u -m research.exp_HB_trend_hold
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                        # noqa: E402
from engine import indicators as ind                         # noqa: E402
from engine.candles import resample                          # noqa: E402
from research import lib                                     # noqa: E402
from research.exp_T4_params import alt_bases                 # noqa: E402
from research.exp_T_supertrend_search import _ym, segments   # noqa: E402

FEE = 0.0005
BAR = 4 * 3_600_000
RULES = ("B1", "B2", "B3")
SHIFTS = 1000


def regime(b4, b1d):
    """4h 봉마다 B1·B2 신호(그 봉 종가 시각에 이미 닫힌 봉 기준)."""
    end = b4.open_time + BAR
    c = b1d.close
    sma = np.full(len(c), np.nan)
    if len(c) >= 200:
        cs_ = np.cumsum(np.concatenate(([0.0], c)))
        sma[199:] = (cs_[200:] - cs_[:-200]) / 200
    k = np.searchsorted(b1d.open_time + 86_400_000, end, side="right") - 1
    b1 = np.where(k >= 0, (c[np.clip(k, 0, None)] > sma[np.clip(k, 0, None)]), False)
    _, d = ind.supertrend(b4.high, b4.low, b4.close, 14, 2.5)
    b2 = np.nan_to_num(d) > 0
    return {"B1": b1, "B2": b2, "B3": b1 & b2}


def fund_per_bar(sym, b4):
    """4h 봉 t 종가→t+1 종가 사이 정산 펀딩 합(롱이 내는 쪽 +)."""
    f = np.zeros(len(b4.close))
    try:
        fs = cs.funding_schedule(sym, int(b4.open_time[0]), int(b4.open_time[-1]) + 2 * BAR)
    except Exception:
        fs = {}
    if fs:
        ft = np.array(list(fs.keys()), dtype=np.int64)
        fr = np.array(list(fs.values()))
        k = (ft - b4.open_time[0] - BAR - 1) // BAR      # 정산이 (t 종가, t+1 종가] 에 들면 봉 t 에 귀속
        ok = (k >= 0) & (k < len(f))
        np.add.at(f, k[ok], fr[ok])
    return f


def curve(pos, r, fund, fee=FEE, funding=True):
    """pos[t]∈{0,1}: 봉 t 종가에 정한 보유 → 봉 t+1 수익. 반환 봉별 수익(길이 n-1)."""
    p = pos[:-1].astype(float)
    turn = np.abs(np.diff(np.concatenate(([0.0], p))))
    return p * r - turn * fee - (p * fund[:-1] if funding else 0.0)


def stats(pr, bars_per_year=6 * 365):
    eq = np.cumprod(1 + pr)
    yrs = len(pr) / bars_per_year
    tot = (eq[-1] - 1) * 100
    ann = (max(eq[-1], 1e-12) ** (1 / yrs) - 1) * 100
    mdd = (1 - eq / np.maximum.accumulate(eq)).max() * 100
    return tot, ann, mdd, (ann / mdd if mdd > 0 else np.nan)


def main() -> int:
    segs = segments()
    st = cs.stats("BTCUSDT")
    base = lib.load("BTCUSDT", start_ms=st["min"], end_ms=st["max"], with_funding=False)[0]
    b4, b1d = resample(base, 240), resample(base, 1440)
    reg = regime(b4, b1d)
    r = b4.close[1:] / b4.close[:-1] - 1.0
    fund = fund_per_bar("BTCUSDT", b4)
    hold_fut = curve(np.ones(len(b4.close), bool), r, fund)
    hold_spot = curve(np.ones(len(b4.close), bool), r, fund, funding=False)
    cuts = [int(np.searchsorted(b4.open_time, a)) for a, _ in segs] + [len(r)]
    print(f"HB · 추세가 좋을 때만 1배 롱 · BTCUSDT 4h · {_ym(int(b4.open_time[0]))}~{_ym(int(b4.open_time[-1]))}")
    print(f"  {'':16} {'전체 수익':>10} {'연':>7} {'MDD':>6} {'Calmar':>7} {'보유비율':>7} {'전환/년':>7} | 구간별 수익 · MDD")

    def row(name, pr, frac=None, sw=None):
        tot, ann, mdd, cal = stats(pr)
        segs_ = []
        for i in range(3):
            t_, _, m_, _ = stats(pr[cuts[i]:cuts[i + 1]])
            segs_.append(f"{t_:+6.0f}%·{m_:3.0f}%")
        extra = f"{frac:6.0%} {sw:7.1f}" if frac is not None else f"{'100%':>6} {'0':>7}"
        print(f"  {name:16} {tot:+9.0f}% {ann:+6.1f}% {mdd:5.0f}% {cal:7.2f} {extra} | " + "  ".join(segs_))
        return tot, ann, mdd, cal, [stats(pr[cuts[i]:cuts[i + 1]])[2] for i in range(3)]

    hf = row("보유(선물·펀딩)", hold_fut)
    hs = row("보유(현물)", hold_spot)
    yrs = len(r) / (6 * 365)
    out = {}
    rng = np.random.default_rng(0)
    for rule in RULES:
        pos = reg[rule]
        frac = pos[:-1].mean()
        sw = np.abs(np.diff(pos.astype(int))).sum() / yrs
        res = row(f"{rule} 선물", curve(pos, r, fund), frac, sw)
        row(f"{rule} 현물(참고)", curve(pos, r, fund, funding=False), frac, sw)
        null = np.empty(SHIFTS)
        n = len(pos)
        for k in range(SHIFTS):
            sh = int(rng.integers(n // 10, n - n // 10))
            null[k] = stats(curve(np.roll(pos, sh), r, fund))[3]
        out[rule] = (res, float((null < res[3]).mean() * 100), float(np.percentile(null, 95)))
        print(f"  {'':16} └ 랜덤 보유(같은 비율·전환) Calmar p95 {np.percentile(null, 95):.2f} · 이 규칙 백분위 {out[rule][1]:.0f}")

    print("\n  알트 55개(2021-01~, 1h→4h·1d, 펀딩 실데이터)")
    alt_rows = {rule: [] for rule in RULES}
    for s, b, _ in alt_bases():
        b1h = b.__class__(b.open_time - 59 * 60_000, b.open, b.high, b.low, b.close, b.volume, 60, taker_buy=b.taker_buy)
        a4, a1d = resample(b1h, 240), resample(b1h, 1440)
        rg = regime(a4, a1d)
        ra = a4.close[1:] / a4.close[:-1] - 1.0
        fa = fund_per_bar(s, a4)
        _, _, hmdd, hcal = stats(curve(np.ones(len(a4.close), bool), ra, fa))
        htot = stats(curve(np.ones(len(a4.close), bool), ra, fa))[0]
        for rule in RULES:
            tot, _, mdd, cal = stats(curve(rg[rule], ra, fa))
            alt_rows[rule].append((s, tot, mdd, cal, htot, hmdd, hcal))
    print(f"  {'규칙':4} {'MDD<보유':>9} {'Calmar>보유':>11} {'수익 중앙':>9} {'보유 수익 중앙':>12} {'MDD 중앙':>8} {'보유 MDD 중앙':>12}")
    for rule in RULES:
        a = alt_rows[rule]
        m_better = np.mean([x[2] < x[5] for x in a]) * 100
        c_better = np.mean([(np.nan_to_num(x[3], nan=-9) > np.nan_to_num(x[6], nan=-9)) for x in a]) * 100
        print(f"  {rule:4} {m_better:8.0f}% {c_better:10.0f}% {np.median([x[1] for x in a]):+8.0f}% "
              f"{np.median([x[4] for x in a]):+11.0f}% {np.median([x[2] for x in a]):7.0f}% {np.median([x[5] for x in a]):11.0f}%")
        res, pct, p95 = out[rule]
        ok1 = all(m < h for m, h in zip(res[4], hf[4])) and m_better >= 80
        ok2 = res[3] > hf[3] and c_better > 50
        ok3 = pct >= 95
        print(f"       판정 ① MDD {'✅' if ok1 else '❌'} · ② Calmar {'✅' if ok2 else '❌'} · ③ 랜덤 보유 대비 {'✅' if ok3 else '❌'}"
              f" (백분위 {pct:.0f}) → {'✅ 통과' if ok1 and ok2 and ok3 else '❌'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
