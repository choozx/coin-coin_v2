"""XSL — XS MOM 을 2020~2023 (규칙을 정할 때 안 본 구간)에 그대로 (사용자: "기간 넓혀서 다시 해봐", 2026-11-23).

지금 규칙(28일 · 상·하위 10% · 7일 보유 · 상장 ≥ 30일 · 거래대금 ≥ $5M)은 2024-01 ~ 2026-11 만 보고 정했다.
2020~2023 은 코로나 폭락(2020-03) · 상승장(2021) · LUNA(2022-05) · FTX(2022-11) · 긴 하락장이 들어 있는, 한 번도 안 본 구간이다.
데이터는 research/xs_long_data.py(폐지 심볼 포함 전체 680개 · 펀딩 중복 없음).

━━ 사전 고정 (2026-11-23 작성 · 결과 보기 전) ━━
규칙    engine.xs_mom 그대로(아무것도 바꾸지 않음) · XSD 일 단위 회계 · 실제 펀딩 · 회전 0.10% · 1배 · 대상 20개 미만인 주는 현금.
표본 밖 OOS = 2020-01-01 ~ 2023-12-31 · 요일 분산(7몫 평균, 실매매 예정 방식)으로 판정.
판정    ① 연 수익 > 0 ② t(일 수익 평균 / 표준편차 × √일수) ≥ 2.0 ③ 7개 요일 하나씩 돌려도 5개 이상 연 > 0 ④ 2020·2021·2022·2023 중 3개 이상 > 0.
        넷 다 → '안 본 구간에서도 재현' · ①만 → '약하게 남음' · ① 실패 → '2024~ 에만 있던 효과일 수 있음, 기대치를 크게 낮춤'.
참고    같은 데이터로 2024-01 ~ 2026-11 재현(기존 +44.9% · 1.38 · −23.3% 와 비교) · 전체 2020~2026 · 연도별 · 대상 코인 수 ·
        2배일 때 최대낙폭·최악의 주/달 · 큰 사건 달(2020-03 · 2021-05 · 2022-05 · 2022-11)의 주별 수익.

    .venv/bin/python -u research/exp_XSL_long.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import warnings
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.xs_mom import AGE, COST, MINQ  # noqa: E402
from research.exp_XSD_weekday import stats  # noqa: E402
from research.xs_long_data import DB, END, FROM  # noqa: E402

warnings.filterwarnings("ignore", category=RuntimeWarning)
D = 86_400_000
NAMES = ("월", "화", "수", "목", "금", "토", "일")


def _ms(y, m, d=1):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


def _dt(t):
    return datetime.fromtimestamp(t / 1000, timezone.utc)


def load():
    c = sqlite3.connect(DB)
    syms = [(s, f) for s, f in c.execute("SELECT symbol, first FROM sym WHERE first IS NOT NULL ORDER BY symbol")]
    idx = {s: j for j, (s, _) in enumerate(syms)}
    days = np.arange(FROM, END + D, D, dtype=np.int64)
    C = np.full((len(days), len(syms)), np.nan)
    Q = np.full((len(days), len(syms)), np.nan)
    for s, t, cl, q in c.execute("SELECT symbol, t, close, qvol FROM k1d"):
        if s in idx and FROM <= t <= END:
            i = (t - FROM) // D
            C[i, idx[s]], Q[i, idx[s]] = cl, q
    Fd = np.zeros((len(days), len(syms)))
    for s, t, r in c.execute("SELECT symbol, t, rate FROM fund"):
        if s in idx and FROM <= t < END + D:
            Fd[(t - FROM) // D, idx[s]] += r
    c.close()
    first = np.array([f for _, f in syms], dtype=float)
    age = (days[:, None] - first[None, :]) / D
    return days, [s for s, _ in syms], C, Q, Fd, age


def tranche(days, C, Q, Fd, age, wd, start):
    """XSD tranche 와 같은 계산(시작일만 인자)."""
    n, m = C.shape
    out = np.full(n, np.nan)
    nuni = {}
    i = int(np.searchsorted(days, start))
    while _dt(days[i]).weekday() != wd:
        i += 1
    w = np.zeros(m)
    for i in range(i, n - 7, 7):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - 28]) & (age[i] >= AGE)
        live &= np.nan_to_num(np.nanmedian(Q[i - 29:i + 1], axis=0)) >= MINQ
        js = np.where(live)[0]
        nuni[int(days[i])] = len(js)
        nw = np.zeros(m)
        if len(js) >= 20:
            sig = C[i, js] / C[i - 28, js] - 1
            k = max(1, int(len(js) * 0.10))
            order = np.argsort(-sig)
            nw[js[order[:k]]] = 0.5 / k
            nw[js[order[-k:]]] = -0.5 / k
        turn = np.abs(nw - w).sum()
        held = np.where(nw != 0)[0]
        for h in range(1, 8):
            d = i + h
            pr = C[d, held] / C[d - 1, held] - 1
            pr = np.where(np.isfinite(pr), pr, 0.0)
            alive = np.isfinite(C[d, held])
            day = float((nw[held] * (pr - np.where(alive, Fd[d, held], 0.0))).sum())
            out[d] = day - (turn * COST if h == 1 else 0.0)
        w = nw
    return out, nuni


def tstat(r):
    r = r[np.isfinite(r)]
    return r.mean() / r.std(ddof=1) * np.sqrt(len(r))


def mdd(r, lev=1.0):
    eq = np.cumprod(1 + lev * r)
    return (eq / np.maximum.accumulate(eq) - 1).min()


def worst(days, r, lev, fmt):
    by = {}
    for t, x in zip(days, r):
        k = _dt(t).strftime(fmt)
        by[k] = by.get(k, 1.0) * (1 + lev * x)
    k = min(by, key=by.get)
    return k, by[k] - 1


def main():
    days, syms, C, Q, Fd, age = load()
    print(f"심볼 {len(syms)}개 · 일봉 {_dt(days[0]):%Y-%m-%d} ~ {_dt(days[-1]):%Y-%m-%d}\n")
    T, U = {}, {}
    for wd in range(7):
        T[wd], U[wd] = tranche(days, C, Q, Fd, age, wd, _ms(2020, 1, 1))
    ok = np.all([np.isfinite(T[w]) for w in range(7)], axis=0)
    P = np.mean([T[w] for w in range(7)], axis=0)
    unis = sorted(U[0].items())
    print("■ 대상 코인 수(월요일 리밸런스, 연도별 중앙 · 20개 미만 주 수)")
    for y in range(2020, 2027):
        v = [n for t, n in unis if _dt(t).year == y]
        print(f"  {y}: 중앙 {np.median(v):4.0f}개 · 20개 미만 {sum(x < 20 for x in v)}주 / {len(v)}주")

    def report(name, a, b):
        sel = ok & (days >= a) & (days < b)
        r = P[sel]
        s = stats(r)
        print(f"\n■ {name}  {_dt(days[sel][0]):%Y-%m-%d} ~ {_dt(days[sel][-1]):%Y-%m-%d} · {sel.sum()}일")
        print(f"  요일 분산  연 {s['ann']*100:+6.1f}% · 샤프 {s['sharpe']:.2f} · t {tstat(r):.2f} · 최대낙폭 1배 {s['mdd']*100:+.1f}% · 2배 {mdd(r, 2)*100:+.1f}%")
        wk, wv = worst(days[sel], r, 2, "%G-W%V")
        mo, mv = worst(days[sel], r, 2, "%Y-%m")
        print(f"  2배 최악의 주 {wk} {wv*100:+.1f}% · 최악의 달 {mo} {mv*100:+.1f}%")
        per = [stats(T[w][sel]) for w in range(7)]
        print("  요일 하나씩 연 " + " · ".join(f"{NAMES[w]} {per[w]['ann']*100:+.0f}%" for w in range(7)) +
              f"  (+ 인 요일 {sum(p['ann'] > 0 for p in per)}/7) · 낙폭 중앙 {np.median([p['mdd'] for p in per])*100:+.1f}%")
        return s, r, sel, per

    s_oos, r_oos, sel_oos, per_oos = report("표본 밖 2020~2023", _ms(2020, 1, 1), _ms(2024, 1, 1))
    report("재현 2024 ~ 2026-11 (기존 데이터: 연 +44.9% · 샤프 1.38 · 낙폭 −23.3%)", _ms(2024, 1, 10), _ms(2026, 11, 11))
    report("전체 2020 ~ 2026-11", _ms(2020, 1, 1), _ms(2026, 11, 17))

    print("\n■ 연도별(요일 분산, 1배 복리)")
    yrs = {}
    for y in range(2020, 2027):
        sel = ok & np.array([_dt(t).year == y for t in days])
        if sel.any():
            yrs[y] = np.prod(1 + P[sel]) - 1
            print(f"  {y}: {yrs[y]*100:+6.1f}% · 낙폭 {mdd(P[sel])*100:+.1f}% · 샤프 {stats(P[sel])['sharpe']:+.2f}")
    print("\n■ 큰 사건 달의 주별 수익(요일 분산, 1배)")
    for y, m, name in ((2020, 3, "코로나 폭락"), (2021, 5, "5월 폭락"), (2022, 5, "LUNA"), (2022, 11, "FTX")):
        sel = ok & np.array([(_dt(t).year, _dt(t).month) == (y, m) for t in days])
        wk = {}
        for t, x in zip(days[sel], P[sel]):
            k = _dt(t - (_dt(t).weekday()) * D).strftime("%m-%d 주")
            wk[k] = wk.get(k, 1.0) * (1 + x)
        print(f"  {y}-{m:02d} {name}: 달 {(np.prod(1 + P[sel]) - 1)*100:+.1f}% · " + " · ".join(f"{k} {(v-1)*100:+.1f}%" for k, v in wk.items()))

    print("\n■ 판정(표본 밖 2020~2023 · 요일 분산)")
    c1 = s_oos["ann"] > 0
    c2 = tstat(r_oos) >= 2.0
    c3 = sum(p["ann"] > 0 for p in per_oos) >= 5
    c4 = sum(yrs.get(y, -1) > 0 for y in (2020, 2021, 2022, 2023)) >= 3
    v = [c1, c2, c3, c4]
    print(f"  ① 연 > 0 ② t ≥ 2.0 ③ 5/7 요일 + ④ 4개 연도 중 3개 + → [{''.join('○' if x else '×' for x in v)}]")
    print("  → " + ("안 본 구간에서도 재현 ✅" if all(v) else "약하게 남음 △" if c1 else "2024~ 에만 있던 효과일 수 있음 ❌"))


if __name__ == "__main__":
    main()
