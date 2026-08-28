"""[J] 리밸런싱 시뮬레이션 — 캐리의 진짜 자본효율과 비용.

I 까지의 숫자는 '증거금이 늘 충분하다'는 가정이었다. 실제로는 가격이 오르면 선물 숏이 손실을
내고 증거금이 녹는다. 2020 년 BTC 는 연중 +306% 올랐다 — 리밸런싱 없이 버티려면 명목의
3 배를 증거금으로 미리 넣어야 하고, 그러면 자본효율이 무너져 수익률이 반토막 난다.

실제 운용은 **현물 다리 이익을 선물 증거금으로 옮기는 것**이다. 그런데 현물은 BTC 라
팔아야 USDT 가 되고, 팔면 델타 중립이 깨져 **숏도 같은 만큼 줄여야 한다.**
→ 가격이 오를수록 포지션이 자연히 축소되고, 명목이 줄면 **펀딩 수익도 줄어든다.**
이 동학과 그 비용이 이 실험의 대상이다.

모델(1h 스텝, 마크투마켓):
    상태: qty(현물BTC = 숏BTC, 델타중립), margin(선물 USDT)
    매 스텝  margin += qty × (P_prev − P_now)        # 숏 손익 실시간 정산
    펀딩시각 margin += qty × P × rate                 # 숏이 펀딩 수취(양수일 때)
    증거금이 유지증거금의 SAFETY 배 아래로 → 리밸런싱:
        현물 일부 매도(수수료 0.10%) → USDT 를 margin 으로
        델타 유지 위해 숏도 같은 수량 커버(수수료 0.05%) → 명목 축소
    margin ≤ 유지증거금 → 청산(그 해 실패로 기록)

돌리기:  python3 -m research.exp_J_rebalance
"""
from __future__ import annotations

import datetime as dt
import sqlite3

import numpy as np

from engine import candle_store as cs

SYMBOL = "BTCUSDT"
MMR = 0.004          # 유지증거금률 0.4% (BTCUSDT 소액 구간)
SPOT_FEE = 0.0010    # 현물 taker 0.10%
FUT_FEE = 0.0005     # 선물 taker 0.05%
# 트리거는 '명목 대비 증거금 비율'로 잡는다. 유지증거금(명목의 0.4%) 배수로 잡으면
# 증거금이 명목의 1% 언저리까지 녹은 뒤에야 발동해서, 1시간 급등 한 번에 청산된다.
# 실무도 마찬가지로 '증거금률이 X% 아래면 보충'으로 관리한다.
REBAL_AT = 0.15      # 증거금 / 명목 이 값 아래로 내려가면 리밸런싱
REBAL_TO = 0.35      # 리밸런싱 후 회복 목표 비율


def _load():
    conn = sqlite3.connect(f"file:{cs.DB_PATH}?mode=ro", uri=True)
    px = conn.execute("SELECT open_time, close FROM candle WHERE symbol=? "
                      "AND open_time % 3600000 = 0 ORDER BY open_time", (SYMBOL,)).fetchall()
    fr = conn.execute("SELECT funding_time, rate FROM funding WHERE symbol=? "
                      "ORDER BY funding_time", (SYMBOL,)).fetchall()
    conn.close()
    return px, dict(fr)


def simulate(times, prices, funding, lev):
    """연초 진입 → 연말 청산. 반환 dict(수익률·리밸런싱 횟수·비용·명목축소·청산여부)."""
    p0 = prices[0]
    qty = 1.0                       # 현물 1 BTC = 숏 1 BTC (명목 p0)
    margin = p0 * qty / lev         # 선물 증거금
    capital = p0 * qty + margin     # 투입 자본(현물 매수대금 + 증거금)
    cost = p0 * qty * SPOT_FEE + p0 * qty * FUT_FEE      # 진입 수수료
    funding_got, rebal, rebal_cost = 0.0, 0, 0.0

    for i in range(1, len(times)):
        p_prev, p = prices[i - 1], prices[i]
        margin += qty * (p_prev - p)                     # 숏 마크투마켓
        r = funding.get(times[i])
        if r is not None:                                # 펀딩 수취(음수면 지불)
            got = qty * p * r
            margin += got
            funding_got += got
        notional = qty * p
        if margin <= notional * MMR:                     # 청산
            return {"liquidated": True, "ret": -1.0, "rebal": rebal,
                    "cost": cost + rebal_cost, "qty_end": qty, "capital": capital}
        if margin < notional * REBAL_AT:                 # 리밸런싱: 현물→증거금
            need = notional * REBAL_TO - margin
            sell = min(qty * 0.99, need / p)             # 팔 BTC (현물 전량은 못 팜)
            margin += sell * p * (1 - SPOT_FEE)          # 매도대금 입금(현물 수수료)
            margin -= sell * p * FUT_FEE                 # 델타 유지: 숏 커버 수수료
            rebal_cost += sell * p * (SPOT_FEE + FUT_FEE)
            qty -= sell                                  # 현물·숏 동시 축소 → 명목 감소
            rebal += 1

    p_end = prices[-1]
    exit_cost = qty * p_end * (SPOT_FEE + FUT_FEE)       # 청산 수수료(양다리)
    equity = qty * p_end + margin - exit_cost            # 현물 가치 + 선물 잔고
    return {"liquidated": False, "ret": equity / capital - 1.0, "rebal": rebal,
            "cost": cost + rebal_cost + exit_cost, "qty_end": qty, "capital": capital,
            "funding": funding_got / capital}


def main():
    px, funding = _load()
    times = np.array([t for t, _ in px])
    prices = np.array([p for _, p in px])
    years = np.array([dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc).year for t in times])

    print(f"\n[J] {SYMBOL} 델타중립 캐리 · 리밸런싱 시뮬레이션 (1h, 마크투마켓)")
    print(f"유지증거금률 {MMR*100:.1f}% · 리밸런싱: 증거금률 {REBAL_AT*100:.0f}% 미만 → "
          f"{REBAL_TO*100:.0f}% 로 회복 · 수수료 현물 {SPOT_FEE*100:.2f}% / 선물 {FUT_FEE*100:.2f}%")

    for lev in (1, 2, 3, 5):
        print(f"\n{'='*96}\n선물 레버리지 {lev}x  (초기 자본 = 현물 1.00 + 증거금 {1/lev:.2f} = {1+1/lev:.2f})")
        print(f"  {'연도':<6} {'수익률':>9} {'그중 펀딩':>10} {'리밸런싱':>8} {'명목 잔존':>9} "
              f"{'총비용(자본%)':>13} {'결과':>8}")
        print("  " + "-" * 88)
        rets = []
        for y in sorted(set(years)):
            m = years == y
            if m.sum() < 200:
                continue
            r = simulate(times[m], prices[m], funding, lev)
            if r["liquidated"]:
                print(f"  {y:<6} {'—':>9} {'—':>10} {r['rebal']:>8} {'—':>9} {'—':>13} {'💥 청산':>8}")
                rets.append(-1.0)
                continue
            rets.append(r["ret"])
            print(f"  {y:<6} {r['ret']*100:>+8.2f}% {r['funding']*100:>+9.2f}% {r['rebal']:>8} "
                  f"{r['qty_end']*100:>8.1f}% {r['cost']/r['capital']*100:>12.2f}% {'ok':>8}")
        ok = [x for x in rets if x > -1.0]
        print("  " + "-" * 88)
        print(f"  평균(청산 제외) {sum(ok)/len(ok)*100:+.2f}%/년" if ok else "  전부 청산")
        if len(ok) < len(rets):
            print(f"  ⚠️ {len(rets)-len(ok)}/{len(rets)} 개 연도 청산")

    print(f"\n{'='*96}")
    print("읽는 법: 명목 잔존이 낮을수록 리밸런싱으로 포지션이 많이 깎였다는 뜻이고,")
    print("        그만큼 펀딩을 덜 받는다. 자본효율(레버리지)과 생존은 정면 트레이드오프다.\n")


if __name__ == "__main__":
    main()
