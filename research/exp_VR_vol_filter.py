"""VR — RSI+CVD 다이버전스에 '고변동' 필터: 안 본 코인 6개로 확인 (사전 고정).

배경(2026-10-14): 변동성 분위 관찰(BACKLOG 'CV' 아래, 사후) — RSI+CVD 롱은 진입 시점 5m ATR/가격 상위 20% 에서만
거래당 gross 가 컸다(BTC 구간1·2 +47.8/+29.7 · SOL +43.6 · ETH +8.3bp, 나머지는 −3.8~+13.3bp). 단 BTC 2024-05~ 는 −8.7bp.
이미 본 데이터에서 나온 관찰이라 **새 코인으로 숫자 그대로** 확인한다.

필터(엔진 `ATR_RANK`, 인과적): 현재 NATR(14) 가 직전 lookback=2016 봉(5m 7일) NATR 중 몇 백분위인가 ≥ 80.
  ※ 관찰은 '데이터 전체 기간 분위'였다 — 실전에서 쓸 수 있는 건 과거만 보는 순위라 그쪽으로 판정한다.
진입: RSI+CVD 다이버전스 롱(엔진 RSI_DIV cvd=1, 프리셋과 같다). 청산: 전저점 20 · 2.5R · 48봉. gross 거래당 bp, net = taker 0.05% 왕복 1배.
코인(이 연구에서 5m 로 본 적 없음): XRP · DOGE · BNB · AVAX · LINK · ADA, 5m, 2022-01-01 ~ 2026-08-31.
  기간 나눔: 앞 2022-01~2024-04 / 뒤 2024-05~2026-08 (BTC 구간3 과 같은 경계 — 최근 약화 확인용).

판정(결과 전 고정):
  ① 필터 효과: 필터 켬 거래당 bp > 필터 끔 — 6 코인 중 ≥ 5
  ② 경제 크기: 필터 켬 거래당 bp ≥ 12 — 6 코인 중 ≥ 4
  ③ 최근: 뒤 기간(2024-05~) 필터 켬 거래당 bp > 0 — 6 코인 중 ≥ 4
  셋 다 → '쓸 만한 필터'. ①만 → '정보는 있지만 돈이 안 됨'. 참고: BTC·ETH·SOL(이미 본 데이터)도 같이 찍는다(판정 밖).

    python3 -u -m research.exp_VR_vol_filter            # 판정
    python3 -u -m research.exp_VR_vol_filter --collect  # 6 코인 1분봉 수집(순차, 메인넷 공개 klines)
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view as swv

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                   # noqa: E402
from engine import indicators as ind                    # noqa: E402
from engine.candles import resample                     # noqa: E402
from research import lib                                # noqa: E402
from research.exp_EX2_swing_rr import run               # noqa: E402

COINS = ("XRPUSDT", "DOGEUSDT", "BNBUSDT", "AVAXUSDT", "LINKUSDT", "ADAUSDT")
SEEN = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
_ms = lambda s: int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)
START, SPLIT, END = _ms("2022-01-01"), _ms("2024-05-01"), _ms("2026-08-31")
RANK_MIN, LOOKBACK = 80.0, 2016
NR, T, LB, FEE = 2.5, 48, 20, 0.0005
MIN_BP = 12.0


def collect():
    for s in COINS:
        t0 = time.time()
        b = cs.ensure(s, START, END, verbose=True)
        print(f"{s}: {len(b.close):,}봉 ({time.time() - t0:.0f}초)", flush=True)


def trades(b):
    """(진입 idx, 필터 켬 여부, 봉별 수익) — 엔진 지표 그대로."""
    sig = ind.rsi_divergence(b.high, b.low, b.close, volume=b.volume, taker_buy=b.taker_buy, cvd_mode=1)
    rank = ind.atr_rank(b.high, b.low, b.close, 14, LOOKBACK)
    n = len(b.close)
    lw = np.full(n, np.nan)
    lw[LB - 1:] = swv(b.low, LB).min(axis=1)
    idx = np.nonzero(sig == 1)[0]
    idx = idx[(idx < n - T - 1) & np.isfinite(rank[idx])]
    return idx, rank[idx] >= RANK_MIN, lw


def stat(b, idx, lw):
    if len(idx) == 0:
        return 0, np.nan, np.nan
    pr = run(b, idx, lw, NR, T)[0]
    net = (np.clip(1 + pr - 2 * FEE, 0, None).prod() - 1) * 100
    return len(pr), pr.mean() * 1e4, net


def main():
    print(f"VR · RSI+CVD 다이버전스 롱 + ATR_RANK(14, {LOOKBACK}) ≥ {RANK_MIN:g} · 5m · 전저점 20 · 2.5R · 48봉 · gross bp · net=taker 0.05% 1배")
    rows = {}
    for s in COINS + SEEN:
        base = lib.load(s, start_ms=START, end_ms=END, with_funding=False)[0]
        b = resample(base, 5)
        idx, on, lw = trades(b)
        late = b.open_time[idx] >= SPLIT
        r = {"끔": stat(b, idx, lw), "켬": stat(b, idx[on], lw),
             "켬·앞": stat(b, idx[on & ~late], lw), "켬·뒤": stat(b, idx[on & late], lw),
             "끔·뒤": stat(b, idx[late], lw)}
        rows[s] = r
        tag = "" if s in COINS else "  (이미 본 데이터 — 판정 밖)"
        print(f"  {s:9} 필터 끔 {r['끔'][0]:4d}건 {r['끔'][1]:+6.1f}bp ({r['끔'][2]:+6.1f}%) | 켬 {r['켬'][0]:4d}건 {r['켬'][1]:+6.1f}bp "
              f"({r['켬'][2]:+6.1f}%) | 켬·앞 {r['켬·앞'][1]:+6.1f}bp ({r['켬·앞'][0]}) · 켬·뒤 {r['켬·뒤'][1]:+6.1f}bp ({r['켬·뒤'][0]}) "
              f"· 끔·뒤 {r['끔·뒤'][1]:+6.1f}bp{tag}", flush=True)
    new = [rows[s] for s in COINS]
    k1 = sum(r["켬"][1] > r["끔"][1] for r in new)
    k2 = sum(r["켬"][1] >= MIN_BP for r in new)
    k3 = sum(r["켬·뒤"][1] > 0 for r in new)
    print(f"\n  판정 ① 필터가 bp 를 올림 {k1}/6 {'✅' if k1 >= 5 else '❌'} · ② 켬 ≥ {MIN_BP:g}bp {k2}/6 {'✅' if k2 >= 4 else '❌'} · "
          f"③ 뒤 기간 켬 > 0 {k3}/6 {'✅' if k3 >= 4 else '❌'}")
    verdict = ("✅ 쓸 만한 필터" if k1 >= 5 and k2 >= 4 and k3 >= 4
               else "△ 정보는 있지만 돈이 안 됨" if k1 >= 5 else "❌ 새 코인에서 재현 안 됨")
    print(f"  → {verdict}")


if __name__ == "__main__":
    collect() if "--collect" in sys.argv else main()
