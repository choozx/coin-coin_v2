"""① 과거 스프레드 추정 — 평온기 호가(1.35bp)가 변동성 구간에도 유효한가.

과거 호가창은 받을 수 없다(바이낸스는 depth 히스토리를 안 준다). 대신 **Corwin-Schultz
고가-저가 스프레드 추정량**을 쓴다(2012, JF): 연속 두 기간의 H/L 범위에서 스프레드를 역산한다.
거래가 없어도 H/L 은 남으므로 5.7년 전체에 적용 가능하다.

왜 필요한가: `xs_liquidity.py` 는 **지금 이 순간** 스냅샷이고 반스프레드 중앙 1.35bp 였다.
그런데 M 이 진입하는 순간은 대개 **변동성이 터진 때**다(펀딩이 극단으로 벌어지는 시점).
평온기 숫자로 비용을 잡으면 낙관 편향이 된다.

    python3 -u -m research.xs_spread_hist
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import numpy as np

from engine import candle_store as cs


def corwin_schultz(h, l):
    """연속 2기간 H/L → 스프레드(비율). 음수 추정치는 0 으로(원논문 권고)."""
    h, l = np.asarray(h, float), np.asarray(l, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        b = np.log(h[1:] / l[1:]) ** 2 + np.log(h[:-1] / l[:-1]) ** 2      # β
        h2 = np.maximum(h[1:], h[:-1]); l2 = np.minimum(l[1:], l[:-1])
        g = np.log(h2 / l2) ** 2                                            # γ
        k = 3.0 - 2.0 * np.sqrt(2.0)
        a = (np.sqrt(2.0 * b) - np.sqrt(b)) / k - np.sqrt(g / k)            # α
        s = 2.0 * (np.exp(a) - 1.0) / (1.0 + np.exp(a))
    return np.where(np.isfinite(s), np.maximum(s, 0.0), np.nan)


def main():
    conn = sqlite3.connect(cs.DB_PATH)
    syms = sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM candle WHERE symbol LIKE '%\\_1H' ESCAPE '\\'"))
    times = None; H = []; L = []; C = []; V = []
    for s in syms:
        rows = conn.execute("SELECT open_time,high,low,close,volume FROM candle "
                            "WHERE symbol=? ORDER BY open_time", (s,)).fetchall()
        if times is None:
            times = np.array([r[0] for r in rows], dtype=np.int64)
        H.append([r[1] for r in rows]); L.append([r[2] for r in rows])
        C.append([r[3] for r in rows]); V.append([r[4] for r in rows])
    conn.close()
    H, L, C, V = map(lambda x: np.array(x, float), (H, L, C, V))
    print(f"Corwin-Schultz 스프레드 추정 · {len(syms)}심볼 × {len(times):,}봉(1h)\n")

    sp = np.vstack([corwin_schultz(H[i], L[i]) for i in range(len(syms))])   # [S × T-1]
    t2 = times[1:]
    half_bp = sp / 2.0 * 10_000                                              # 반스프레드(bp)

    # 변동성 레짐 = 그 시각 전 심볼 평균 |1h 수익률|
    ret = np.abs(C[:, 1:] / C[:, :-1] - 1.0)
    vol = np.nanmean(ret, axis=0)
    q = np.nanpercentile(vol, [50, 90, 99])
    med = np.nanmedian(half_bp, axis=0)          # 시각별 심볼 중앙 반스프레드
    p90 = np.nanpercentile(half_bp, 90, axis=0)  # 꼬리(얇은 심볼)

    print(f"{'레짐':22s} {'시각수':>8s} {'반스프레드 중앙':>14s} {'꼬리(p90)':>11s}")
    for lab, m in (("평온 (변동성 하위 50%)", vol <= q[0]),
                   ("보통 (50~90%)", (vol > q[0]) & (vol <= q[1])),
                   ("변동성 상위 10%", (vol > q[1]) & (vol <= q[2])),
                   ("극단 상위 1%", vol > q[2])):
        print(f"{lab:22s} {m.sum():8,d} {np.nanmedian(med[m]):13.2f}bp {np.nanmedian(p90[m]):10.2f}bp")

    print(f"\n연도별 (반스프레드 중앙)")
    for y in range(2021, 2027):
        lo = int(datetime(y, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        hi = int(datetime(y + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        m = (t2 >= lo) & (t2 < hi)
        if m.sum() < 100:
            continue
        print(f"  {y}  {np.nanmedian(med[m]):6.2f}bp   꼬리 {np.nanmedian(p90[m]):6.2f}bp")

    now = np.nanmedian(med[-24 * 30:])
    print(f"\n최근 30일 추정 {now:.2f}bp  vs  xs_liquidity 실측 스냅샷 1.35bp "
          f"→ 추정량이 {'과대' if now > 1.35 else '과소'} ({now/1.35:.1f}배)")
    print("""
※ 한계 — 이 추정량은 **정작 필요한 구간에서 깨진다.** γ(2기간 범위)가 β 보다 커지면 α 가 음수가
   되어 0 으로 잘리는데, 변동성이 터질수록 그 조건이 성립한다. 위 표에서 '변동성 상위 10%' 가
   평온기보다 낮고 '극단 1%' 가 0.00bp 인 게 그 증상이다. **레짐별 비용은 이걸로 답할 수 없다.**
   답하려면 depth 스냅샷을 시간에 걸쳐 직접 모아야 한다(수집 시작 시점부터만 가능).

※ 쓸 수 있는 건 **연도별 상대 추이**다(편향이 대략 일정하니 비율은 의미가 있다):
   2021 이 2023~2026 의 약 2배 → 백테스트 비용을 2021-22 구간에서 2배로 잡는 게 맞다.""")


if __name__ == "__main__":
    main()
