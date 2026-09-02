"""호가 깊이로 슬리피지 추정 — M(펀딩 횡단면)이 현실적으로 굴러가는가.

M 은 편도 비용에 극도로 민감하다(실측: 5bp 면 6/7 조합 생존, 15bp 면 2개, 30bp 면 전멸).
그런데 우리는 알트 퍼프의 실제 슬리피지를 재본 적이 없다. **그게 유일한 미지수**다.

여기서는 **주문 없이** 호가창(fapi/v1/depth)만 떠서, 주어진 주문 크기가 호가를 얼마나
먹는지 계산한다. 무료고 즉시 되며, 결과가 나쁘면 실돈 논의 자체가 불필요해진다.

한계(정직하게): **지금 이 순간의 스냅샷**이라 과거 5.7년을 대표하지 않는다. 다만
'지금 굴릴 수 있는가'에는 답한다. 그리고 M 은 극단 펀딩 심볼을 고르므로 — 그건 종종
쏠림이 심한 얇은 코인이다 — **평균이 아니라 꼬리(최악 심볼)가 비용을 지배한다.**

    python3 -u -m research.xs_liquidity
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone

import numpy as np

from research.xs_data import universe

DEPTH = "https://fapi.binance.com/fapi/v1/depth"
TAKER_FEE_BP = 5.0                       # 편도 수수료(슬리피지와 합쳐야 실제 비용)
PORTFOLIOS = (10_000, 100_000, 1_000_000, 10_000_000)   # 자본(USDT)
K = 11                                   # 한쪽 심볼 수 → 총 2K 포지션
GROSS = 2.0                              # 롱 1 + 숏 1


def book(symbol: str, limit: int = 500):
    q = f"?symbol={symbol}&limit={limit}"
    req = urllib.request.Request(DEPTH + q, headers={"User-Agent": "auto-trading-research/0.1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
    bids = np.array([[float(p), float(v)] for p, v in d["bids"]])
    asks = np.array([[float(p), float(v)] for p, v in d["asks"]])
    return bids, asks


def slip_bp(asks, notional):
    """시장가 매수로 notional(USDT) 를 채울 때 중간가 대비 슬리피지(bp). 깊이 부족이면 None."""
    mid_ref = asks[0, 0]
    got = cost = 0.0
    for price, qty in asks:
        take = min(qty * price, notional - cost)
        if take <= 0:
            break
        got += take / price
        cost += take
        if cost >= notional - 1e-9:
            break
    if cost < notional - 1e-6:
        return None                      # 호가창 500단으로도 못 채움
    vwap = cost / got
    return (vwap / mid_ref - 1.0) * 10_000


def main():
    uni = [s for s, _ in universe(datetime(2021, 1, 1, tzinfo=timezone.utc))]
    print(f"호가 스냅샷 {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · {len(uni)}심볼 "
          f"· 한쪽 {K}개(총 {2*K} 포지션)\n")
    books, half = {}, {}
    for i, s in enumerate(uni, 1):
        try:
            b, a = book(s)
            books[s] = a
            half[s] = (a[0, 0] / b[0, 0] - 1.0) * 10_000 / 2.0     # 반스프레드(bp)
        except Exception as e:
            print(f"  {s}: 실패 {e}")
        time.sleep(0.12)
    print(f"  수집 {len(books)}심볼 · 반스프레드 중앙 {np.median(list(half.values())):.2f}bp "
          f"· 최악 {max(half.values()):.2f}bp ({max(half, key=half.get)})\n")

    print(f"{'자본':>12s} {'포지션당':>10s} | {'슬리피지 중앙':>12s} {'상위25%':>9s} "
          f"{'최악':>9s} | {'편도 총비용(중앙)':>16s} {'(꼬리)':>9s}")
    print("-" * 92)
    for cap in PORTFOLIOS:
        per = cap * GROSS / (2 * K)
        # ★ `v or 999` 로 쓰면 안 된다 — 슬리피지가 정확히 0.0(첫 호가로 다 채움)일 때
        #   falsy 라서 '체결 불가'로 뒤집힌다. 실제로 이 버그로 소액 구간이 1000bp 로 나왔다.
        sl = []
        for s, a in books.items():
            v = slip_bp(a, per)
            sl.append(half[s] + (999.0 if v is None else v))
        sl = np.array(sl)
        med, q75, worst = np.median(sl), np.percentile(sl, 75), sl.max()
        # M 은 극단 펀딩 심볼을 고른다 → 평균이 아니라 상위 25% 쪽이 현실에 가깝다
        print(f"{cap:>11,d}$ {per:>9,.0f}$ | {med:>11.1f}bp {q75:>8.1f}bp {worst:>8.1f}bp | "
              f"{TAKER_FEE_BP+med:>15.1f}bp {TAKER_FEE_BP+q75:>8.1f}bp")

    print(f"\n판정 기준(M 슬리피지 민감도 실측): 편도 총비용")
    print(f"  ≤15bp → M 유효(연 7~13%)   ~15bp → 경계   ≥30bp → M 기각")
    print(f"\n※ 슬리피지 = 반스프레드 + 호가 잠식. 999bp 는 500단으로도 못 채운 심볼.")


if __name__ == "__main__":
    main()
