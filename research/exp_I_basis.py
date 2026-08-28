"""[I] 베이시스 실측 — H(펀딩 캐리) 상한에서 무엇이 깎이는가.

H 는 '선물-현물 괴리 0' 을 가정한 상한이었다(캐시에 현물이 없었으므로). 이제 현물 1h 를
받았으니(`research/spot_data.py`) 실제 괴리를 재고, 캐리 손익에 반영한다.

델타중립 캐리의 가격 손익(펀딩 제외):
    진입: 선물 숏 @F0, 현물 롱 @S0     청산: 선물 커버 @F1, 현물 매도 @S1
    손익 ≈ (F0-F1)/F0 + (S1-S0)/S0  ≈  b0 - b1      (b = F/S - 1, 베이시스)
즉 **베이시스가 축소되면 이익, 확대되면 손실.** 캐리의 진짜 리스크는 '진입 시 b 가 높고
청산 시 b 가 낮기를' 바라는 것이 아니라, **b 가 벌어진 채로 청산을 강요당하는 것**이다.

돌리기:  python3 -m research.exp_I_basis
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import numpy as np

from engine import candle_store as cs
from research.spot_data import spot_symbol

SYMBOL = "BTCUSDT"
ROUND_TRIP = 2 * 0.0005 + 2 * 0.0010     # H 와 동일: 선물 왕복 + 현물 왕복 = 0.30%
CAPITAL = 1.0 + 1.0 / 3                  # H 와 동일: 명목1 + 증거금1/3


def _series():
    """선물·현물 1h 종가를 시각으로 짝지어 (times, fut, spot) 반환."""
    conn = sqlite3.connect(f"file:{cs.DB_PATH}?mode=ro", uri=True)
    q = ("SELECT open_time, close FROM candle WHERE symbol=? AND open_time %% 3600000 = 0 "
         "ORDER BY open_time")
    fut = dict(conn.execute(q.replace("%%", "%"), (SYMBOL,)).fetchall())
    spo = dict(conn.execute(q.replace("%%", "%"), (spot_symbol(SYMBOL),)).fetchall())
    conn.close()
    ts = sorted(set(fut) & set(spo))
    return (np.array(ts), np.array([fut[t] for t in ts]), np.array([spo[t] for t in ts]))


def _funding_by_year():
    conn = sqlite3.connect(f"file:{cs.DB_PATH}?mode=ro", uri=True)
    rows = conn.execute("SELECT funding_time, rate FROM funding WHERE symbol=? ORDER BY funding_time",
                        (SYMBOL,)).fetchall()
    conn.close()
    out = {}
    for t, r in rows:
        out.setdefault(dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc).year, []).append(r)
    return out


def main():
    ts, fut, spo = _series()
    b = (fut / spo - 1.0) * 100                     # 베이시스 %
    years = np.array([dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc).year for t in ts])
    print(f"\n[I] {SYMBOL} 베이시스 (선물/현물 − 1), 1h 종가 {len(ts):,}개 짝지음")
    print(f"  전체: 평균 {b.mean():+.4f}%  중앙 {np.median(b):+.4f}%  "
          f"표준편차 {b.std():.4f}%  범위 [{b.min():+.3f}%, {b.max():+.3f}%]")
    print(f"  |b|>0.5% 인 시간 비율: {(np.abs(b) > 0.5).mean()*100:.2f}%  "
          f"· |b|>1% : {(np.abs(b) > 1.0).mean()*100:.2f}%")

    print(f"\n{'='*94}\n연도별 베이시스와 '상시 캐리' 실제 손익 (연초 진입 → 연말 청산)")
    print(f"  {'연도':<6} {'평균b':>8} {'연초b':>8} {'연말b':>8} {'b0−b1':>8} "
          f"{'Σ펀딩':>9} {'베이시스손익':>11} {'왕복비용':>9} {'순(자본%)':>10}")
    print("  " + "-" * 90)
    fund = _funding_by_year()
    nets_h, nets_i = [], []
    for y in sorted(set(years)):
        m = years == y
        if m.sum() < 100 or y not in fund:
            continue
        by = b[m]
        b0, b1 = by[0], by[-1]
        pnl_basis = b0 - b1                          # 베이시스 축소분이 이익
        f = sum(fund[y]) * 100
        net_i = (f + pnl_basis - ROUND_TRIP * 100) / CAPITAL
        net_h = (f - ROUND_TRIP * 100) / CAPITAL     # H 의 상한(베이시스 0 가정)
        nets_h.append(net_h)
        nets_i.append(net_i)
        print(f"  {y:<6} {by.mean():>+7.3f}% {b0:>+7.3f}% {b1:>+7.3f}% {pnl_basis:>+7.3f}% "
              f"{f:>+8.2f}% {pnl_basis:>+10.3f}% {ROUND_TRIP*100:>8.2f}% {net_i:>+9.2f}%")
    print("  " + "-" * 90)
    print(f"  {'평균':<6} {'':>8} {'':>8} {'':>8} {'':>8} {'':>9} {'':>11} {'':>9} "
          f"{sum(nets_i)/len(nets_i):>+9.2f}%")
    print(f"\n  H 상한(베이시스 0 가정) 평균: {sum(nets_h)/len(nets_h):+.2f}%/년")
    print(f"  I 실측(베이시스 반영)  평균: {sum(nets_i)/len(nets_i):+.2f}%/년  "
          f"→ 차이 {sum(nets_i)/len(nets_i) - sum(nets_h)/len(nets_h):+.2f}%p")

    print(f"\n{'='*94}\n[강제청산 리스크] 베이시스가 벌어진 채 나가야 하면 얼마를 잃나")
    for p in (50, 90, 99, 99.9):
        print(f"  b 의 {p:>4}% 분위수: {np.percentile(b, p):+.3f}%   "
              f"(그 시점에 청산하면 진입가 대비 그만큼 불리)")
    print("\n※ 베이시스는 평균회귀 성향이 강하지만, 급등장에선 선물 프리미엄이 크게 벌어진다.")
    print("  캐리의 진짜 위험은 펀딩이 아니라 '벌어진 상태에서 청산을 강요당하는 것'.\n")


if __name__ == "__main__":
    main()
