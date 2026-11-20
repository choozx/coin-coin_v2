"""QB — 분기물 베이시스 고정: 현물 매수 + 분기 선물 매도, 만기까지 보유 (사용자: "작게 많은 곳에서 확실하게", 2026-11-20).

펀딩 캐리(H·CC)는 매 정산 펀딩이 바뀐다 → 수익이 흔들린다. 분기물은 **진입 순간 수익률이 확정**된다(만기에 선물가 = 현물가로 수렴).
가격 방향 위험 없음(현물 1 + 선물 −1). 남는 위험 = 거래소 · 증거금(코인M 은 현물 코인을 증거금으로 쓰면 1배 자본으로 헤지).

━━ 사전 고정 (2026-11-20 작성 · 결과 보기 전) ━━
계약    바이낸스 코인M 분기물 BTCUSD · ETHUSD (만기 3·6·9·12월 마지막 금요일 08:00 UTC) · 2021-03 ~ 지금 만기분.
        데이터: data.binance.vision futures/cm 월별 1d 클라인(만기 지난 계약 포함) · 현물 = 바이낸스 현물 BTCUSDT·ETHUSDT 1d 종가.
진입    매월 1일 00:00 UTC 종가 기준(전날 일봉 종가), 그때 만기까지 30일 이상 남은 **가장 가까운** 분기물.
보유    만기까지(만기 정산 = 현물 지수로 수렴 → 수익 = 진입 베이시스).
수익    기간 수익 = 선물/현물 − 1 − 비용 · 연환산 = 기간 수익 × 365 / 남은 일수.
비용    0.30%(현물 매수 0.10 + 선물 매도 0.05 + 만기 정산 0.015 + 현물 매도 0.10 + 여유) — 만기 보유라 왕복 한 번.
판정    ① 진입의 90% 이상이 비용 후 > 0  ② 연환산 중앙 > 무위험 4%  ③ 연도별 연환산 중앙이 모두 > 0
        ④ 같은 기간 BTC·ETH 펀딩 캐리(무기한, 매 8h 펀딩 합 − 같은 비용)보다 연환산 중앙이 높거나 같다.
참고    분기물 vs 무기한 펀딩: 같은 진입·같은 기간 실제 펀딩 합.

    .venv/bin/python -u research/exp_QB_quarterly_basis.py
"""
from __future__ import annotations

import csv
import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

import numpy as np

D = 86_400_000
COST = 0.0030
RF = 0.04
COINS = ("BTC", "ETH")


def _get(url, raw=False):
    for i in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research/0.1"}), timeout=60) as r:
                b = r.read()
                return b if raw else json.loads(b)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 * (i + 1))
        except urllib.error.URLError:
            time.sleep(2 * (i + 1))
    return None


def last_friday(y, m):
    d = datetime(y, m + 1, 1, tzinfo=timezone.utc) - timedelta(days=1) if m < 12 else datetime(y, 12, 31, tzinfo=timezone.utc)
    while d.weekday() != 4:
        d -= timedelta(days=1)
    return d.replace(hour=8)


def expiries():
    out = []
    for y in range(2021, 2028):
        for m in (3, 6, 9, 12):
            e = last_friday(y, m)
            if datetime(2021, 3, 1, tzinfo=timezone.utc) <= e <= datetime.now(timezone.utc) + timedelta(days=200):
                out.append(e)
    return out


def fut_daily(coin, exp):
    sym = f"{coin}USD_{exp:%y%m%d}"
    closes = {}
    start = exp - timedelta(days=200)
    y, m = start.year, start.month
    while (y, m) <= (exp.year, exp.month):
        b = _get(f"https://data.binance.vision/data/futures/cm/monthly/klines/{sym}/1d/{sym}-1d-{y}-{m:02d}.zip", raw=True)
        if b:
            with zipfile.ZipFile(io.BytesIO(b)) as z, z.open(z.namelist()[0]) as f:
                for row in csv.reader(io.TextIOWrapper(f)):
                    if row and row[0].isdigit():
                        closes[int(row[0]) // D * D] = float(row[4])
        m += 1
        if m == 13:
            y, m = y + 1, 1
    if not closes and exp > datetime.now(timezone.utc):        # 아직 거래 중 → 공개 API
        rows = _get(f"https://dapi.binance.com/dapi/v1/klines?symbol={sym}&interval=1d&limit=1000") or []
        closes = {int(r[0]) // D * D: float(r[4]) for r in rows}
    return sym, closes


def spot_daily(coin):
    out, st = {}, int(datetime(2020, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
    while True:
        rows = _get(f"https://api.binance.com/api/v3/klines?symbol={coin}USDT&interval=1d&startTime={st}&limit=1000")
        if not rows:
            break
        out.update({int(r[0]): float(r[4]) for r in rows})
        if len(rows) < 1000:
            break
        st = int(rows[-1][0]) + D
    return out


def funding_sum(coin, a, b):
    """같은 기간 무기한(USDT-M) 펀딩 합 — 캐리 비교용."""
    tot, st = 0.0, a
    while st < b:
        rows = _get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={coin}USDT&startTime={st}&endTime={b}&limit=1000") or []
        time.sleep(0.6)
        if not rows:
            break
        tot += sum(float(r["fundingRate"]) for r in rows)
        if len(rows) < 1000:
            break
        st = int(rows[-1]["fundingTime"]) + 1
    return tot


def main():
    exps = expiries()
    now = datetime.now(timezone.utc)
    trades = []
    for coin in COINS:
        spot = spot_daily(coin)
        fut = {e: fut_daily(coin, e) for e in exps}
        print(f"{coin}: 분기물 {sum(1 for e in exps if fut[e][1])}개 데이터 · 현물 {len(spot)}일", flush=True)
        d = datetime(2021, 1, 1, tzinfo=timezone.utc)
        while d < now:
            t = int(d.timestamp() * 1000) - D                    # 전날 일봉(종가 = d 00:00)
            cand = [e for e in exps if (e - d).days >= 30 and fut[e][1].get(t)]
            if cand and spot.get(t):
                e = min(cand)
                sym, fc = fut[e]
                days = (e - d).total_seconds() / 86400
                raw = fc[t] / spot[t] - 1
                net = raw - COST
                done = e <= now
                fund = funding_sum(coin, int(d.timestamp() * 1000), int(e.timestamp() * 1000)) if done else None
                trades.append(dict(coin=coin, d=d, sym=sym, days=days, raw=raw, net=net, ann=net * 365 / days, done=done,
                                   fund=fund, fund_ann=(fund - COST) * 365 / days if fund is not None else None))
            m = d.month + 1
            d = d.replace(year=d.year + (m == 13), month=1 if m == 13 else m)
    done = [x for x in trades if x["done"]]
    open_ = [x for x in trades if not x["done"]]
    print(f"\n진입 {len(trades)}회(만기 끝 {len(done)} · 진행 중 {len(open_)}) · 비용 {COST*100:.2f}% · 무위험 {RF*100:.0f}%\n")
    print(f"{'코인':4} {'진입':10} {'계약':14} {'남은일':>5} {'베이시스':>8} {'비용 후':>7} {'연환산':>7} | {'같은 기간 펀딩 캐리 연환산':>10}")
    for x in trades:
        print(f"{x['coin']:4} {x['d']:%Y-%m-%d} {x['sym']:14} {x['days']:5.0f} {x['raw']*100:+7.2f}% {x['net']*100:+6.2f}% {x['ann']*100:+6.1f}% | "
              + (f"{x['fund_ann']*100:+6.1f}%" if x["fund_ann"] is not None else "  (진행 중)"))
    ann = np.array([x["ann"] for x in done])
    pos = np.mean([x["net"] > 0 for x in done])
    by_y = {}
    for x in done:
        by_y.setdefault(x["d"].year, []).append(x["ann"])
    fa = np.array([x["fund_ann"] for x in done])
    ok = [pos >= 0.9, np.median(ann) > RF, all(np.median(v) > 0 for v in by_y.values()), np.median(ann) >= np.median(fa)]
    print(f"\n■ 만기까지 끝난 {len(done)}회: 비용 후 플러스 {pos:.0%} · 연환산 중앙 {np.median(ann)*100:+.1f}% · 평균 {ann.mean()*100:+.1f}% · "
          f"최저 {ann.min()*100:+.1f}% · 최고 {ann.max()*100:+.1f}%")
    print("  연도별 연환산 중앙: " + " · ".join(f"{y} {np.median(v)*100:+.1f}%({len(v)})" for y, v in sorted(by_y.items())))
    print(f"  같은 진입·기간 펀딩 캐리: 연환산 중앙 {np.median(fa)*100:+.1f}% · 평균 {fa.mean()*100:+.1f}% · 분기물이 높은 비율 {np.mean(ann > fa):.0%}")
    oks = ["① 90%↑ 플러스", "② 중앙 > 무위험 4%", "③ 연도별 > 0", "④ 펀딩 캐리 이상"]
    print(f"\n■ 판정: {'✅ 합격' if all(ok) else '❌'}  " + " · ".join(f"{o} {'○' if k else '×'}" for o, k in zip(oks, ok)))
    if open_:
        last = {}
        for x in open_:
            last[x["coin"]] = x                                  # 코인별 가장 최근 진입
        print("\n■ 지금 진입하면(진행 중 계약, 확정 수익): " + " · ".join(f"{x['coin']} {x['sym']} {x['ann']*100:+.1f}%/년({x['days']:.0f}일)"
                                                       for x in last.values()))


if __name__ == "__main__":
    main()
