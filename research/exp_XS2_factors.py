"""XS2 — XS MOM 과 같은 틀(주간 알트 횡단면 롱숏, 시장 중립)에서 '고르는 기준'만 바꾼 팩터 5개 (사용자: "XS MOM 정도 수익 나는 전략 또", 2026-11-20).

목적: XS MOM 만큼 벌고, 가능하면 MOM 과 덜 같이 움직여(상관 낮음) 함께 돌릴 때 낙폭을 줄이는 두 번째 전략.

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
틀      exp_XS_cross_section 과 같다: USDT 무기한(xs_sym, 폐지 포함) 일봉 2024-01 ~ 2026-11-16 · 상장 ≥ 30일 · 30일 일 거래대금 중앙 ≥ $5M ·
        월요일 종가 리밸런스 · 7일 보유 · 상·하위 10% 동일 가중 롱/숏(총 노출 1, 1배) · 실제 펀딩 반영 · 회전당 0.10% · 폐지면 마지막 종가.
팩터(신호가 높을수록 롱)
  RMOM  잔차 모멘텀: 28일 수익률 − β × BTC 28일 수익률 (β = 직전 60일 일 수익률을 BTC 에 회귀). 시장 따라 오른 몫을 뺀 '자기 힘'.
  MOM7  7일 모멘텀(7일 수익률).
  VLM   거래 관심: 7일 평균 거래대금 / 60일 평균 거래대금 (높으면 롱).
  LVOL  저변동: −(30일 일 수익률 표준편차) — 덜 흔들리는 코인 롱 · 많이 흔들리는 코인 숏.
  MAX   복권 회피: −(28일 중 최대 일 수익률) — 하루 폭등한 적 없는 코인 롱 · 폭등했던 코인 숏.
판정(팩터별, XS 와 같은 네 개 · 5개 동시라 t 문턱을 2.4 → 2.6):
  ① t ≥ 2.6  ② 25H2 · 26H1 · 26H2 모두 > 0  ③ 최고 주 빼도 누적 > 0  ④ 상·하위 20%(5분위)도 연 > 0
참고(판정 아님): MOM 과 주간 수익 상관 · MOM 과 반반 섞었을 때 연 수익·t·최대낙폭 · 롱/숏 다리 · 비용 0.
데이터: `exp_XS_cross_section.collect()`(일봉) + 이 파일 `--funding`(펀딩 → xvenue.db fh5_funding, XS 와 같은 표).

    .venv/bin/python -u research/exp_XS2_factors.py --collect    # 일봉(XS 그대로)
    .venv/bin/python -u research/exp_XS2_factors.py --funding    # 펀딩
    .venv/bin/python -u research/exp_XS2_factors.py              # 판정
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research import exp_XS_cross_section as xs  # noqa: E402

D = xs.D
KINDS = ("RMOM", "MOM7", "VLM", "LVOL", "MAX")
T_MIN = 2.6


def collect_funding():
    c = sqlite3.connect(xs.DB, timeout=60)
    c.execute("CREATE TABLE IF NOT EXISTS fh5_funding (symbol TEXT, t INTEGER, rate REAL, PRIMARY KEY (symbol, t))")
    c.execute("CREATE TABLE IF NOT EXISTS xs2_fdone (symbol TEXT PRIMARY KEY)")
    done = {r[0] for r in c.execute("SELECT symbol FROM xs2_fdone")}
    syms = [r[0] for r in c.execute("SELECT symbol FROM xs_sym") if r[0] not in done and r[0].isascii()]
    print(f"펀딩 받을 심볼 {len(syms)}", flush=True)
    for n, s in enumerate(syms, 1):
        st, rows = xs.FETCH_FROM, []
        while st < xs.END + D:
            for i in range(5):
                try:
                    u = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={s}&startTime={st}&endTime={xs.END + D}&limit=1000"
                    with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "research/0.1"}), timeout=30) as r:
                        got = json.load(r)
                    break
                except urllib.error.HTTPError as e:
                    got = []
                    if e.code == 400:
                        break
                    time.sleep(30 if e.code in (418, 429) else 2)
                except urllib.error.URLError:
                    got = []
                    time.sleep(2)
            time.sleep(0.65)                                   # fundingRate 5분 500회
            if not got:
                break
            rows += [(s, int(x["fundingTime"]), float(x["fundingRate"])) for x in got]
            if len(got) < 1000:
                break
            st = int(got[-1]["fundingTime"]) + 1
        c.executemany("INSERT OR IGNORE INTO fh5_funding VALUES (?,?,?)", rows)
        c.execute("INSERT OR IGNORE INTO xs2_fdone VALUES (?)", (s,))
        c.commit()
        if n % 50 == 0:
            print(f"  {n}/{len(syms)}", flush=True)
    c.close()


def signal(kind, C, Q, i, js, bj):
    if kind == "MOM":
        return C[i, js] / C[i - 28, js] - 1
    if kind == "MOM7":
        return C[i, js] / C[i - 7, js] - 1
    r = C[i - 60:i + 1] / C[i - 61:i] - 1                    # 61 일 → 60개 일 수익률
    if kind == "RMOM":
        b = r[:, bj]
        x = r[:, js]
        ok = ~np.isnan(x) & ~np.isnan(b)[:, None]
        bb = np.where(ok, b[:, None], np.nan)
        cov = np.nanmean((x - np.nanmean(x, 0)) * (bb - np.nanmean(bb, 0)), 0)
        beta = cov / np.nanvar(bb, 0)
        return (C[i, js] / C[i - 28, js] - 1) - np.nan_to_num(beta) * (C[i, bj] / C[i - 28, bj] - 1)
    if kind == "VLM":
        return np.nanmean(Q[i - 6:i + 1, js], 0) / (np.nanmean(Q[i - 59:i + 1, js], 0) + 1)
    if kind == "LVOL":
        return -np.nanstd(r[-30:, js], 0)
    if kind == "MAX":
        return -np.nanmax(r[-28:, js], 0)
    raise ValueError(kind)


def run(days, syms, C, Q, Fd, age, kind, frac=0.10, cost=xs.COST, legs="both"):
    """exp_XS_cross_section.run 과 같은 회계(주간·7일 보유·펀딩·폐지·회전 비용), 신호만 바꾼다."""
    n, m = C.shape
    bj = syms.index("BTCUSDT")
    i0 = max(int(np.searchsorted(days, xs.START)), 62)        # 60일 수익률 계산에 필요한 과거 확보
    while datetime.fromtimestamp(days[i0] / 1000, timezone.utc).weekday() != 0:
        i0 += 1                                                # ★ 그다음 월요일 — XS 와 같은 요일(처음엔 max 를 뒤에 걸어 요일이 밀렸다)
    w = np.zeros(m)
    out = []
    for i in range(i0, n - 7, 7):
        live = ~np.isnan(C[i]) & ~np.isnan(C[i - 28]) & ~np.isnan(C[i - 7]) & (age[i] >= xs.AGE)
        live &= np.nan_to_num(np.nanmedian(Q[i - 29:i + 1], axis=0)) >= xs.MINQ
        js = np.where(live)[0]
        if len(js) < 20:
            continue
        sig = signal(kind, C, Q, i, js, bj)
        good = np.isfinite(sig)
        js, sig = js[good], sig[good]
        if len(js) < 20:
            continue
        k = max(1, int(len(js) * frac))
        order = np.argsort(-sig)
        L, S = js[order[:k]], js[order[-k:]]
        nw = np.zeros(m)
        if legs in ("both", "long"):
            nw[L] = 0.5 / k
        if legs in ("both", "short"):
            nw[S] = -0.5 / k
        turn = np.abs(nw - w).sum()
        r = np.zeros(m)
        for j in np.where(nw != 0)[0]:
            seg = C[i:i + 8, j]
            last = np.where(~np.isnan(seg))[0][-1]
            r[j] = seg[last] / seg[0] - 1 - Fd[i + 1:i + 1 + last, j].sum()
        out.append((int(days[i]), (nw * r).sum() - turn * cost, turn))
        w = nw
    return out


def stats(res):
    t = np.array([x[0] for x in res]); r = np.array([x[1] for x in res])
    eq = np.cumprod(1 + r)
    return dict(t=t, r=r, ann=r.mean() * 52, tst=r.mean() / (r.std(ddof=1) / np.sqrt(len(r))),
                mdd=(eq / np.maximum.accumulate(eq) - 1).min(), win=np.mean(r > 0))


def main():
    if "--collect" in sys.argv:
        xs.collect()
        return
    if "--funding" in sys.argv:
        collect_funding()
        return
    days, syms, C, Q, Fd, age = xs.load()
    print(f"심볼 {len(syms)}개 · {len(days)}일 · 펀딩 기록 있는 심볼 {int((Fd != 0).any(0).sum())}")
    mom = stats(run(days, syms, C, Q, Fd, age, "MOM"))
    print(f"기준 MOM(재현): 연 {mom['ann']*100:+.1f}% · t {mom['tst']:+.2f} · 최대낙폭 {mom['mdd']*100:+.1f}%  (XS 기록 +63.1% · t 2.66 · −20.9%)")
    mr = dict(zip(mom["t"], mom["r"]))
    passed = []
    for kind in KINDS:
        res = run(days, syms, C, Q, Fd, age, kind)
        q5 = stats(run(days, syms, C, Q, Fd, age, kind, frac=0.20))["ann"]
        ok = xs.judge(res, f"{kind} · 사전 고정(t 문턱 {T_MIN})", extra=q5)
        s = stats(res)
        ok = ok and s["tst"] >= T_MIN                         # judge 는 2.4 로 본다 → 여기서 2.6 재확인
        both = [d for d in s["t"] if d in mr]
        a = np.array([dict(zip(s["t"], s["r"]))[d] for d in both]); b = np.array([mr[d] for d in both])
        mix = stats([(d, 0.5 * x + 0.5 * y, 0) for d, x, y in zip(both, a, b)])
        print(f"   판정: {'✅ 합격' if ok else '❌ 불합격'}")
        print(f"   MOM 과 주간 상관 {np.corrcoef(a, b)[0, 1]:+.2f} · MOM 반반 섞기: 연 {mix['ann']*100:+.1f}% · t {mix['tst']:+.2f} · "
              f"최대낙폭 {mix['mdd']*100:+.1f}% (MOM 단독 {mom['mdd']*100:+.1f}%)")
        lo = [stats(run(days, syms, C, Q, Fd, age, kind, legs=lg)) for lg in ("long", "short")]
        c0 = stats(run(days, syms, C, Q, Fd, age, kind, cost=0))
        print(f"   참고: 롱 다리 연 {lo[0]['ann']*100:+.1f}% · 숏 다리 {lo[1]['ann']*100:+.1f}% · 비용 0 {c0['ann']*100:+.1f}%")
        if ok:
            passed.append(kind)
    print(f"\n■ 합격: {passed or '없음'}")


if __name__ == "__main__":
    main()
