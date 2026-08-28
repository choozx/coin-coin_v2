"""[H] 펀딩 캐리(델타 중립) — 방향 예측을 포기한 첫 갈래.

C·A2·B·F·G 가 모두 같은 벽에 막혔다: **방향성 예측으로는 BTC 드리프트를 못 이긴다.**
캐리는 그 벽 바깥에 있다 — 선물 숏 + 현물 롱으로 델타를 0 으로 만들면 가격 방향과 무관하게
8시간마다 펀딩만 받는다. 랜덤 진입 귀무와 애초에 경쟁하지 않는다.

계산 모델(명목 N 달러 기준):
    수익 = N × Σ(펀딩률)  −  진입·청산 수수료(선물 왕복 + 현물 왕복)
    자본 = 현물 N + 선물 증거금 N/L   →  수익률 = 수익 / 자본

★ 이 실험이 측정하지 '못하는' 것 — 결론을 읽을 때 반드시 감안할 것:
  1) **베이시스 리스크.** 캐시에 현물 가격이 없다(선물만). 진입·청산 시점의 선물-현물 괴리가
     수익을 깎거나 더할 수 있다. 여기 숫자는 '괴리 0' 가정의 **상한**이다.
  2) **청산 리스크.** 선물 숏은 가격 급등 시 마진콜. 레버리지를 낮추면 완화되지만 자본이 더 묶인다.
  3) **실행 가능성.** 이 봇은 선물 전용이다. 현물 다리를 붙이려면 별도 배선이 필요하다.
  상한이 비용을 못 넘으면 여기서 닫힌다. 넘으면 그때 위 셋을 따진다.

돌리기:  python3 -m research.exp_H_funding_carry
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from engine import candle_store as cs

SYMBOL = "BTCUSDT"
LEV = 3                     # 선물 숏 다리 레버리지(증거금 = N/LEV)
FUT_TAKER = 0.0005          # 선물 taker 0.05%
SPOT_TAKER = 0.0010         # 현물 taker 0.10% (VIP0, BNB 할인 없음)
ROUND_TRIP = 2 * FUT_TAKER + 2 * SPOT_TAKER      # 양다리 왕복 = 0.30%
CAPITAL = 1.0 + 1.0 / LEV   # 명목 1 당 묶이는 자본(현물 1 + 증거금 1/L)


def _load():
    conn = sqlite3.connect(f"file:{cs.DB_PATH}?mode=ro", uri=True)
    rows = conn.execute("SELECT funding_time, rate FROM funding WHERE symbol=? "
                        "ORDER BY funding_time", (SYMBOL,)).fetchall()
    conn.close()
    return rows


def _year(ms):
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).year


def main():
    rows = _load()
    print(f"\n[H] {SYMBOL} 펀딩 캐리 · 델타중립(선물숏+현물롱) · 선물레버리지 {LEV}x")
    print(f"명목 1 당 자본 {CAPITAL:.2f} · 왕복비용 {ROUND_TRIP*100:.2f}%(선물{FUT_TAKER*100:.2f}%×2 + 현물{SPOT_TAKER*100:.2f}%×2)")

    # ── ① 상시 캐리: 그 해 내내 들고 있기(진입·청산 1회) ──
    print(f"\n{'='*92}\n① 상시 캐리 (연초 진입 → 연말 청산, 왕복비용 1회)")
    print(f"  {'연도':<6} {'펀딩건수':>8} {'Σ펀딩(명목%)':>13} {'순수익(자본%)':>14} "
          f"{'음수구간%':>9} {'최악 8h':>9} {'최대연속지불':>12}")
    print("  " + "-" * 88)
    by_year = {}
    for t, r in rows:
        by_year.setdefault(_year(t), []).append(r)
    totals = []
    for y in sorted(by_year):
        v = by_year[y]
        gross = sum(v) * 100
        net_cap = (sum(v) - ROUND_TRIP) / CAPITAL * 100
        neg = sum(1 for x in v if x < 0) / len(v) * 100
        worst = min(v) * 100
        # 최대 연속 지불(음수 펀딩이 이어진 누적 손실, 명목%)
        run = cur = 0.0
        for x in v:
            cur = min(0.0, cur + x) if x < 0 else 0.0
            run = min(run, cur)
        totals.append(net_cap)
        print(f"  {y:<6} {len(v):>8} {gross:>+12.2f}% {net_cap:>+13.2f}% "
              f"{neg:>8.1f}% {worst:>+8.3f}% {run*100:>+11.2f}%")
    print("  " + "-" * 88)
    print(f"  {'평균':<6} {'':>8} {'':>13} {sum(totals)/len(totals):>+13.2f}%")

    # ── ② 선택적 캐리: 펀딩이 임계 이상일 때만 보유(8h 단위 진입·청산) ──
    print(f"\n{'='*92}\n② 선택적 캐리 (펀딩 ≥ 임계인 구간만 보유 — 8h 마다 들락날락하면 매번 왕복비용)")
    print(f"  {'임계(/8h)':<12} {'보유비율':>8} {'Σ펀딩(명목%)':>13} {'왕복횟수':>8} "
          f"{'비용(명목%)':>12} {'순수익(자본%)':>14}")
    print("  " + "-" * 88)
    for thr in (0.0, 0.0001, 0.0002, 0.0005, 0.0010):
        held = [r for _, r in rows if r >= thr]
        # 연속 보유 구간 수 = 왕복 횟수(구간이 끊길 때마다 청산·재진입)
        trips, prev = 0, False
        for _, r in rows:
            on = r >= thr
            if on and not prev:
                trips += 1
            prev = on
        gross = sum(held) * 100
        cost = trips * ROUND_TRIP * 100
        yrs = (rows[-1][0] - rows[0][0]) / 86400_000 / 365
        net = (gross - cost) / CAPITAL / yrs
        print(f"  {thr*100:>10.3f}% {len(held)/len(rows)*100:>7.1f}% {gross:>+12.2f}% "
              f"{trips:>8} {cost:>11.2f}% {net:>+13.2f}%/년")

    # ── ③ 상시 캐리 전체 기간 요약 ──
    yrs = (rows[-1][0] - rows[0][0]) / 86400_000 / 365
    gross_all = sum(r for _, r in rows) * 100
    net_all = (gross_all - ROUND_TRIP * 100) / CAPITAL / yrs
    print(f"\n{'='*92}\n③ 전체 {yrs:.1f}년 상시 보유: Σ펀딩 {gross_all:+.1f}%(명목) "
          f"− 왕복 {ROUND_TRIP*100:.2f}% → 자본대비 연 {net_all:+.2f}%")
    print("\n※ 베이시스·청산 리스크 미반영 상한. 현물 다리 배선도 아직 없음(선물 전용 봇).\n")


if __name__ == "__main__":
    main()
