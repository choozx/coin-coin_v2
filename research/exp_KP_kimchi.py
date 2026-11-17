"""KP — 김프 헤지: 업비트 현물 롱 + 바이낸스 USDT 무기한 숏, 김프가 평소보다 낮을 때 들어가 돌아오면 나온다.

가설. 김프(업비트 원화가 ÷ (바이낸스 달러가 × 원·달러) − 1)는 국내 자본 규제 때문에 해외 차익거래가
못 메운다 — 경쟁자가 국내 거주자뿐. 같은 코인을 양쪽에 반대로 들면 코인 가격은 상쇄되고 **김프 변화**만 남는다.
코인을 거래소 사이로 옮기지 않는다(송금 규제·지연 회피). 업비트는 공매도가 없어 한 방향(김프 저점 매수)만.

손익 정의 — **달러 기준(판정용)**과 원화 기준을 나눈다.
  원화로 업비트 코인을 사고 달러로 숏을 잡으면 결과적으로 **달러 자산을 들고 있게 된다** → 원화 기준 손익엔
  환율 베팅이 섞인다(원화 약세면 김프와 무관하게 번다). 엣지 판정은 그걸 뺀 달러 기준으로 한다.
  달러 기준 한 거래 수익 = (Pb1·k1 − Pb0·k0) / (Pb0·(1+k0)) + 숏 펀딩 수취 − 비용   (Pb = 바이낸스가, k = 김프)

규칙(룩어헤드 없음, 사전에 고정):
  k 는 1시간봉 종가 · 환율은 **전날** ECB 기준환율(당일 값은 그 시각에 아직 모름).
  신호: 직전 L 일 평균 김프 m. k ≤ m − d 면 진입, k ≥ m 이거나 30일 지나면 청산. 신호 다음 봉 종가에 체결.
  비용: 왕복 합계(업비트 2회 + 바이낸스 2회 수수료·슬리피지) 0.2% / 0.3%.
  L ∈ {7, 30}일 · d ∈ {0.5, 1, 2}%p.  기본 = L 30 · d 1%p · 비용 0.3%.
  자본 = 업비트 원화 1 + 바이낸스 숏 증거금 0.5(2배) = 명목의 1.5배.

판정(사전에 정함):
  ✅ 기본 설정에서 코인 평균 **달러 기준 자본 대비 연 수익 > 4%(무위험)** · 음수 연도 없음 ·
     거래당 평균이 랜덤 진입(같은 보유기간) p95 를 넘는 코인이 과반.

한계: 환율이 일별이라 장중 환율 움직임(하루 0.3~0.5%)이 김프 계산에 잡음으로 들어간다. 업비트↔바이낸스 사이
송금을 안 하므로 오래 들면 계좌 간 잔고가 쏠린다(코인 +50% 면 숏 증거금 부족) — 최대 역행폭으로 따로 본다.

    python3 -u -m research.exp_KP_kimchi                   # sameday (기본)
    python3 -u -m research.exp_KP_kimchi stale             # 처음 버전(손익도 전날 환율 — 부풀려진다)
    python3 -u -m research.exp_KP_kimchi usdt              # 업비트 USDT 시세로 (2024-06~)
    python3 -u -m research.exp_KP_kimchi sameday --since=2024-06-08   # usdt 와 같은 기간 비교
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

from engine import candle_store as cs
from research.xv_data import DB

H = 3_600_000
COINS = ("BTC", "ETH", "XRP", "SOL", "DOGE", "ADA", "LINK", "AVAX")
LS, DS, COSTS = (7, 30), (0.005, 0.01, 0.02), (0.002, 0.003)
DEFAULT = (30, 0.01, 0.003)
HMAX = 30 * 24
CAPITAL = 1.5
RF = 4.0
SAMPLES = 2000


def load(coin, mode="sameday"):
    """(t, Pu, Pb, Xs, Xp, ks, kp, F) — 신호용 환율·김프(s)와 손익용(p)을 나눈다.

    mode
      stale    신호·손익 모두 전날 ECB  ← 처음 버전. 손익까지 전날 환율로 재면 '환율 갱신'이 가짜 수익으로 잡힌다
      sameday  신호 = 전날 ECB(그 시각에 앎) · 손익 = 당일 ECB(회계 — 사후에 알아도 된다)
      usdt     신호·손익 모두 업비트 KRW-USDT 1시간봉(그 시각 실제 시세, 2024-06~)
      hourly   신호·손익 모두 외환시장 원·달러 1시간봉(그 시각 시세, 2024-01~) — 시차 없는 공식 환율
    """
    c = sqlite3.connect(DB)
    up = dict(c.execute("SELECT t, close FROM upbit_1h WHERE coin=?", (coin,)))
    fxd = dict(c.execute("SELECT day, usdkrw FROM fx"))
    fund = defaultdict(float)
    for t, r in c.execute("SELECT t, rate FROM funding WHERE venue='bn_usdt' AND coin=?", (coin,)):
        fund[int(t) // H * H] += float(r)                  # 정산 시각이 든 1시간 칸
    c.close()
    b = sqlite3.connect(cs.DB_PATH)
    bn = dict(b.execute("SELECT open_time, close FROM candle WHERE symbol=?", (f"{coin}USDT_1H",)))
    b.close()
    ts = np.array(sorted(set(up) & set(bn)), dtype=np.int64)
    days = sorted(fxd)
    # 전날(UTC) 이전의 가장 최근 영업일 환율
    def fx_for(t):
        d = (datetime.fromtimestamp(t / 1000, timezone.utc).date() - timedelta(days=1)).isoformat()
        i = np.searchsorted(days, d, side="right") - 1
        return fxd[days[i]] if i >= 0 else np.nan
    def fx_same(t):
        d = datetime.fromtimestamp(t / 1000, timezone.utc).date().isoformat()
        i = np.searchsorted(days, d, side="right") - 1
        return fxd[days[i]] if i >= 0 else np.nan
    if mode == "hourly":                                   # 외환시장 1시간봉 — 그 시각 시세(주말은 금요일 종가 유지)
        c2 = sqlite3.connect(DB)
        fxh = sorted(c2.execute("SELECT t, usdkrw FROM fx_1h"))
        c2.close()
        ft = np.array([t for t, _ in fxh], dtype=np.int64); fv = np.array([v for _, v in fxh])
        ts = ts[ts >= ft[0]]
        Xs = Xp = fv[np.searchsorted(ft, ts, side="right") - 1]
    elif mode == "usdt":
        c2 = sqlite3.connect(DB)
        usdt = dict(c2.execute("SELECT t, close FROM upbit_1h WHERE coin='USDT'"))
        c2.close()
        ts = np.array([t for t in ts if t in usdt], dtype=np.int64)
        Xs = Xp = np.array([usdt[t] for t in ts])
    else:
        c1, c2_ = {}, {}
        Xs = np.array([c1.setdefault(t // 86_400_000, fx_for(t)) for t in ts])
        Xp = Xs if mode == "stale" else np.array([c2_.setdefault(t // 86_400_000, fx_same(t)) for t in ts])
    Pu = np.array([up[t] for t in ts]); Pb = np.array([bn[t] for t in ts])
    ok = np.isfinite(Xs) & np.isfinite(Xp)
    ts, Pu, Pb, Xs, Xp = ts[ok], Pu[ok], Pb[ok], Xs[ok], Xp[ok]
    ks = Pu / (Pb * Xs) - 1.0
    kp = Pu / (Pb * Xp) - 1.0
    F = np.array([fund.get(int(t), 0.0) for t in ts])
    return ts, Pu, Pb, Xs, Xp, ks, kp, F


def trade_ret(i, j, Pu, Pb, X, k, Fc, cost):
    """i 진입·j 청산 — (달러 기준, 원화 기준, 숏 최대 역행)."""
    f = (Fc[j] - Fc[i]) / (1 + k[i])                       # (i, j] 정산분 — 진입 봉 정산은 못 받는다
    usd = (Pb[j] * k[j] - Pb[i] * k[i]) / (Pb[i] * (1 + k[i])) + f - cost
    krw = ((Pu[j] - Pu[i]) - X[j] * (Pb[j] - Pb[i])) / Pu[i] + f * X[j] / X[i] - cost
    adv = Pb[i:j + 1].max() / Pb[i] - 1
    return usd, krw, adv


def simulate(ts, Pu, Pb, Xs, Xp, ks, kp, F, L, d, cost):
    k, X = kp, Xp                                          # 손익은 p, 신호는 ks
    n, w = len(ks), L * 24
    cs_ = np.r_[0.0, np.cumsum(ks)]
    Fc = np.r_[0.0, np.cumsum(F)][1:]                     # Fc[j] = j 까지 누적
    trades, pos, i0 = [], False, 0
    t = w
    while t < n - 1:
        m = (cs_[t] - cs_[t - w]) / w                     # 직전 w 봉(현재 제외) 평균
        if not pos and ks[t] <= m - d:
            pos, i0 = True, t + 1                          # 다음 봉에 체결
        elif pos and (ks[t] >= m or t + 1 - i0 >= HMAX):
            j = t + 1
            trades.append((i0, j) + trade_ret(i0, j, Pu, Pb, X, k, Fc, cost))
            pos = False
        t += 1
    return trades, Fc


def year(t):
    return datetime.fromtimestamp(int(t) / 1000, timezone.utc).year


def null_p95(holds, n_tr, Pu, Pb, X, k, Fc, cost, seed=0):
    rng = np.random.default_rng(seed)
    n = len(k)
    means = np.empty(SAMPLES)
    holds = np.asarray(holds)
    for s in range(SAMPLES):
        h = rng.choice(holds, n_tr)
        i = rng.integers(0, n - h - 1)
        j = i + h
        f = (Fc[j] - Fc[i]) / (1 + k[i])
        means[s] = ((Pb[j] * k[j] - Pb[i] * k[i]) / (Pb[i] * (1 + k[i])) + f - cost).mean()
    return float(np.percentile(means, 95))


def main():
    import sys
    mode = next((a for a in sys.argv[1:] if a in ("stale", "sameday", "usdt", "hourly")), "sameday")
    since = next((a.split("=")[1] for a in sys.argv[1:] if a.startswith("--since=")), None)
    data = {}
    for c in COINS:
        d = load(c, mode)
        if since:
            m = d[0] >= int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp() * 1000)
            d = tuple(x[m] for x in d)
        data[c] = d
    print(f"KP · 김프 헤지(업비트 현물 롱 + 바이낸스 숏) · 환율 {mode}" + (f" · {since}~" if since else "")
          + " · 수익률은 자본(명목×1.5) 대비 연%")
    print("\n0. 김프가 어땠나 (연도별 중앙 · 5~95% 범위)")
    for c, (ts, Pu, Pb, Xs, X, ks, k, F) in data.items():
        yy = np.array([year(t) for t in ts])
        cells = [f"{y} {np.median(k[yy == y])*100:+.1f}({np.percentile(k[yy == y], 5)*100:+.1f}~"
                 f"{np.percentile(k[yy == y], 95)*100:+.1f})" for y in np.unique(yy)]
        print(f"   {c:5} " + " · ".join(cells))

    L0, D0, C0 = DEFAULT
    print(f"\n1. 기본 설정 L={L0}일 · d={D0*100:.0f}%p · 왕복 비용 {C0*100:.1f}%")
    print(f"   {'코인':5} {'거래':>4} {'보유중':>6} {'평균보유':>7} {'승률':>5} {'거래당(달러)':>11} {'랜덤 p95':>8} "
          f"{'연(달러)':>8} {'연(원화)':>8} {'최대역행':>7}")
    summary, by_year, beat = {}, defaultdict(list), 0
    for c, (ts, Pu, Pb, Xs, X, ks, k, F) in data.items():
        tr, Fc = simulate(ts, Pu, Pb, Xs, X, ks, k, F, L0, D0, C0)
        if not tr:
            print(f"   {c:5} 거래 없음"); continue
        usd = np.array([x[2] for x in tr]); krw = np.array([x[3] for x in tr]); adv = max(x[4] for x in tr)
        holds = [j - i for i, j, *_ in tr]
        yrs = (ts[-1] - ts[L0 * 24]) / H / 24 / 365
        a_usd = usd.sum() / yrs / CAPITAL * 100; a_krw = krw.sum() / yrs / CAPITAL * 100
        p95 = null_p95(holds, len(tr), Pu, Pb, X, k, Fc, C0)
        beat += usd.mean() > p95
        summary[c] = a_usd
        for (i, j, u, *_) in tr:
            by_year[year(ts[j])].append((c, u))
        print(f"   {c:5} {len(tr):4d} {sum(holds)/(len(ts)-L0*24):6.0%} {np.mean(holds)/24:6.1f}일 {np.mean(usd > 0):5.0%} "
              f"{usd.mean()*100:+10.2f}% {p95*100:+7.2f}% {a_usd:+7.2f}% {a_krw:+7.2f}% {adv*100:+6.0f}%")
    yr_avg = {}
    for y, v in sorted(by_year.items()):
        per = defaultdict(float)
        for c, u in v:
            per[c] += u
        yr_avg[y] = np.mean([per.get(c, 0.0) for c in summary]) / CAPITAL * 100
    print("   연도별(코인 평균, 달러 기준 자본 대비 %): " + " · ".join(f"{y} {v:+.2f}" for y, v in yr_avg.items()))

    print("\n2. 설정별 코인 평균 연%(달러 기준 자본 대비) — [비용 0.2% | 0.3%]")
    for L in LS:
        cells = []
        for d in DS:
            v = []
            for cost in COSTS:
                a = []
                for c, (ts, Pu, Pb, Xs, X, ks, k, F) in data.items():
                    tr, _ = simulate(ts, Pu, Pb, Xs, X, ks, k, F, L, d, cost)
                    yrs = (ts[-1] - ts[L * 24]) / H / 24 / 365
                    a.append(sum(x[2] for x in tr) / yrs / CAPITAL * 100)
                v.append(np.mean(a))
            cells.append(f"d={d*100:.1f}%p {v[0]:+6.2f} | {v[1]:+6.2f}")
        print(f"   L={L:2d}일: " + "   ".join(cells))

    avg = np.mean(list(summary.values()))
    worst = min(yr_avg.values())
    ok = avg > RF and worst > 0 and beat > len(summary) / 2
    print(f"\n판정: 코인 평균 {avg:+.2f}%/년(무위험 {RF:.0f}%) · 최악 연도 {worst:+.2f}% · 랜덤 p95 초과 {beat}/{len(summary)} "
          f"→ {'✅' if ok else '❌'}")


if __name__ == "__main__":
    main()
