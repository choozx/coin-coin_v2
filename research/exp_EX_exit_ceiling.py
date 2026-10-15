"""EX — 익절·손절로 수익을 만들 수 있나: 같은 청산을 '신호 진입'과 '랜덤 진입'에 똑같이 적용해 비교.

요청(2026-10-14): DV5 기각 뒤 "익절 전략을 잘 해야 될 거 같은데...".
원리: 청산 규칙은 진입 뒤 가격 경로를 수확할 뿐이다. 신호 진입 뒤 경로의 분포가 랜덤 진입 뒤 경로와 같다면
**어떤 청산 규칙이든 기대값이 같다**(가격이 마팅게일이면 어떤 정지 규칙도 기대값을 못 바꾼다 — optional stopping).
그래서 청산 격자를 양쪽에 똑같이 대고 '신호 − 랜덤' 거래당 bp 를 본다. 0 근처면 익절로 풀 문제가 아니다.

진입(롱, BTC 5m 전 기간 2019-09~2026-08):
  · R5   RSI(14) 가 30 아래로 들어가는 봉 종가
  · DV   RSI(14) 30 과매도 + 상승 다이버전스 + 복귀 확인(DV 기본값)
  · 랜덤 아무 봉 종가 50,000개(ATR 유효)
청산(ATR(14) 배수 — 변동성 차이를 정규화): 익절 0.5·1·1.5·2·3·4·6·∞ × 손절 0.5·1·1.5·2·3·4·6·∞ × 시간 48봉 = 64.
  같은 봉에서 둘 다 닿으면 손절 먼저. 체결은 지정 수준(갭 무시 — 양쪽 똑같이). 수수료 0(gross).
⚠ 랜덤 진입은 평온한 시각도 뽑는다 → ATR 배수로 맞췄지만 변동성 구성이 다르다. 그래서 '고변동 랜덤'(ATR/가격 상위 20% 봉)도 같이 본다.

    python3 -u -m research.exp_EX_exit_ceiling
"""
from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                  # noqa: E402
from engine import indicators as ind                   # noqa: E402
from engine.candles import resample                    # noqa: E402
from research import lib                               # noqa: E402
from research.exp_DV_rsi_divergence import signals_long  # noqa: E402

H = 48
KS = (0.5, 1, 1.5, 2, 3, 4, 6, np.inf)
N_RAND = 50_000


def paths(b, idx):
    """진입 idx 별 (고가, 저가, 종가) / 진입가 − 1, 모양 (n, H)."""
    off = idx[:, None] + np.arange(1, H + 1)[None, :]
    e = b.close[idx][:, None]
    return b.high[off] / e - 1, b.low[off] / e - 1, b.close[off] / e - 1


def exit_ret(hi, lo, cl, a, tp, sl):
    """롱. a = ATR/가격(진입 시). 반환 거래별 수익."""
    n = len(a)
    tpl = (tp * a)[:, None]
    sll = (-sl * a)[:, None]
    hit_sl = lo <= sll
    hit_tp = hi >= tpl
    big = H + 1
    t_sl = np.where(hit_sl.any(1), hit_sl.argmax(1), big)
    t_tp = np.where(hit_tp.any(1), hit_tp.argmax(1), big)
    r = cl[:, -1].copy()
    m_sl = (t_sl <= t_tp) & (t_sl < big)
    m_tp = (t_tp < t_sl)
    r[m_sl] = -sl * a[m_sl]
    r[m_tp] = tp * a[m_tp]
    return r


def main():
    st = cs.stats("BTCUSDT")
    b = resample(lib.load("BTCUSDT", start_ms=st["min"], end_ms=st["max"], with_funding=False)[0], 5)
    n = len(b.close)
    rsi = ind.rsi(b.close, 14)
    atr = ind.atr(b.high, b.low, b.close, 14) / b.close
    ok = np.isfinite(atr) & (np.arange(n) < n - H - 1) & (np.arange(n) > 200)
    prev = np.concatenate(([np.nan], rsi[:-1]))
    with np.errstate(invalid="ignore"):
        r5 = np.nonzero((rsi < 30) & (prev >= 30) & ok)[0]
    dv = np.array([t for t, _ in signals_long(b.low.tolist(), rsi.tolist(), 30)], int)
    dv = dv[ok[dv]]
    rng = np.random.default_rng(0)
    pool = np.nonzero(ok)[0]
    rnd = rng.choice(pool, N_RAND, replace=False)
    hv = pool[atr[pool] >= np.percentile(atr[pool], 80)]
    rhv = rng.choice(hv, N_RAND, replace=False)
    sets = {"R5 과매도 진입": r5, "DV 다이버전스": dv, "랜덤": rnd, "랜덤(고변동)": rhv}
    P = {k: (*paths(b, v), atr[v]) for k, v in sets.items()}
    print(f"EX · 같은 청산 64개를 신호·랜덤 진입에 · BTCUSDT 5m · 롱 · gross · 진입 수 "
          + " · ".join(f"{k} {len(v):,}" for k, v in sets.items()))
    print(f"  진입 시 ATR/가격 중앙: " + " · ".join(f"{k} {np.median(atr[v]) * 1e4:.0f}bp" for k, v in sets.items()))

    tab = {}
    for tp in KS:
        for sl in KS:
            tab[(tp, sl)] = {k: float(exit_ret(*P[k][:3], P[k][3], tp, sl).mean() * 1e4) for k in sets}
    for sig in ("R5 과매도 진입", "DV 다이버전스"):
        for base in ("랜덤", "랜덤(고변동)"):
            d = np.array([v[sig] - v[base] for v in tab.values()])
            print(f"\n  {sig} − {base}: 거래당 bp 차이 중앙 {np.median(d):+.1f} · 범위 {d.min():+.1f} ~ {d.max():+.1f}")
        best = max(tab, key=lambda k: tab[k][sig])
        bb = max(tab, key=lambda k: tab[k]["랜덤(고변동)"])
        f = lambda k: f"익절 {k[0]:g}ATR 손절 {k[1]:g}ATR"
        print(f"  {sig} 최고 청산: {f(best)} → {tab[best][sig]:+.1f}bp (같은 청산 랜덤 {tab[best]['랜덤']:+.1f} · 고변동 랜덤 {tab[best]['랜덤(고변동)']:+.1f})")
        print(f"  고변동 랜덤 최고 청산: {f(bb)} → {tab[bb]['랜덤(고변동)']:+.1f}bp")

    print("\n  표: 거래당 gross bp — R5 / DV / 랜덤 / 고변동 랜덤 (행 익절 ATR, 열 손절 ATR, ∞ = 없음)")
    print("  " + " " * 6 + "".join(f"{('손절 ' + format(s, 'g')):>26}" for s in (1, 2, 4, np.inf)))
    for tp in (1, 2, 4, np.inf):
        cells = []
        for sl in (1, 2, 4, np.inf):
            v = tab[(tp, sl)]
            cells.append(f"{v['R5 과매도 진입']:+5.1f}/{v['DV 다이버전스']:+5.1f}/{v['랜덤']:+5.1f}/{v['랜덤(고변동)']:+5.1f}")
        print(f"  익절 {tp:>3g} " + "".join(f"{c:>26}" for c in cells))


if __name__ == "__main__":
    main()
