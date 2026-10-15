"""VD — VR 에서 혼자 강했던 DOGE: 안 본 데이터로 다시 (사전 고정).

요청(2026-10-14): 사용자 "그럼 도지코인으로 처음부터 봐볼까?"
VR(❌)에서 RSI+CVD 롱 + ATR_RANK ≥ 80 이 DOGE 2022-01~2026-08 만 +38.4bp(128건, net +40.9%) — 9 코인 중 최고 1개를 고른 것이라
같은 데이터를 다시 보면 같은 운을 또 확인할 뿐이다. 규칙은 **VR 그대로 한 글자도 안 바꾸고** 안 본 데이터에만 댄다.

  A. DOGE 의 안 본 기간: 상장(2020-07) ~ 2021-12-31. (2021 은 일론 머스크 광풍 — 해석 주의)
  B. '밈코인이라 되는 것' 가설: 1000SHIB · 1000PEPE · WIF, 상장 ~ 2026-08-31 (이 연구에서 처음 봄).

판정(결과 전 고정, gross 거래당 bp):
  A 통과: DOGE 앞 기간 필터 켬 ≥ 12bp  그리고  켬 > 끔  그리고  켬 net(taker 0.05% 1배) > 0
  B 통과: 3 코인 중 ≥ 2 에서 켬 ≥ 12bp  그리고  ≥ 2 에서 켬 > 끔
  A·B 둘 다 → '밈코인 후보'(다음은 실시간 페이퍼) · A 만 → 'DOGE 한정, 근거 약함' · A 실패 → DOGE 2022~ 는 우연으로 본다.

    python3 -u -m research.exp_VD_doge_oos --collect   # 1분봉 수집
    python3 -u -m research.exp_VD_doge_oos             # 판정
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                          # noqa: E402
from engine.candles import resample                            # noqa: E402
from research import lib                                       # noqa: E402
from research.exp_VR_vol_filter import trades, stat, MIN_BP    # noqa: E402

_ms = lambda s: int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)
EARLY = _ms("2020-01-01")                      # 상장 전이면 거래소가 상장일부터 준다
DOGE_END = _ms("2021-12-31T23:59")
MEME_END = _ms("2026-08-31")
MEMES = ("1000SHIBUSDT", "1000PEPEUSDT", "WIFUSDT")


def collect():
    for s, e in (("DOGEUSDT", DOGE_END),) + tuple((m, MEME_END) for m in MEMES):
        t0 = time.time()
        b = cs.ensure(s, EARLY, e, verbose=True)
        print(f"{s}: {len(b.close):,}봉 ({time.time() - t0:.0f}초)", flush=True)


def row(s, end):
    base = lib.load(s, start_ms=EARLY, end_ms=end, with_funding=False)[0]
    b = resample(base, 5)
    idx, on, lw = trades(b)
    first = datetime.fromtimestamp(b.open_time[0] / 1000, timezone.utc).strftime("%Y-%m")
    off_, on_ = stat(b, idx, lw), stat(b, idx[on], lw)
    half = b.open_time[idx] >= b.open_time[len(b.close) // 2]
    h1, h2 = stat(b, idx[on & ~half], lw), stat(b, idx[on & half], lw)
    print(f"  {s:13} {first}~ 필터 끔 {off_[0]:4d}건 {off_[1]:+6.1f}bp ({off_[2]:+6.1f}%) | 켬 {on_[0]:4d}건 {on_[1]:+6.1f}bp "
          f"({on_[2]:+6.1f}%) | 켬 앞 절반 {h1[1]:+6.1f}bp ({h1[0]}) · 뒤 절반 {h2[1]:+6.1f}bp ({h2[0]})", flush=True)
    return off_, on_


def main():
    print("VD · VR 규칙 그대로(RSI+CVD 롱 + ATR_RANK(14,2016) ≥ 80 · 5m · 전저점 20 · 2.5R · 48봉) · gross bp · net=taker 0.05% 1배")
    print(" A. DOGE 안 본 기간")
    off_, on_ = row("DOGEUSDT", DOGE_END)
    a = on_[1] >= MIN_BP and on_[1] > off_[1] and on_[2] > 0
    print(f"  → A {'✅' if a else '❌'} (켬 ≥ {MIN_BP:g}bp · 켬 > 끔 · 켬 net > 0)")
    print(" B. 다른 밈코인")
    rs = [row(m, MEME_END) for m in MEMES]
    k1 = sum(on[1] >= MIN_BP for _, on in rs)
    k2 = sum(on[1] > off[1] for off, on in rs)
    bb = k1 >= 2 and k2 >= 2
    print(f"  → B {'✅' if bb else '❌'} (켬 ≥ {MIN_BP:g}bp {k1}/3 · 켬 > 끔 {k2}/3)")
    v = ("✅ 밈코인 후보 — 다음은 실시간 페이퍼" if a and bb else "△ DOGE 한정, 근거 약함" if a
         else "❌ DOGE 2022~ 결과는 우연으로 본다")
    print(f"\n  → {v}")


if __name__ == "__main__":
    collect() if "--collect" in sys.argv else main()
