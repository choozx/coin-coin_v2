"""TP — 여러 코인 추세추종 포트폴리오. 코인 하나에선 잡음에 묻히는 약한 신호가 분산으로 드러나는가.

동기(2026-10-06, T4): BTC 에서 고른 4h ST(20,4.0) 롱숏을 알트 55개에 숫자 그대로 → 개별 코인은 p95 초과 2/55
(우연 수준)인데 **정밀 귀무 백분위 중앙 78**(무작위면 50). 약하지만 넓게 퍼진 신호일 수 있다. 개별 코인 판정은
분산이 커서 못 잡는다 — 56개에 나눠 담으면 잡음끼리 상쇄돼 신호가 남는지 본다. 사용자 "진행해".

규칙(결과 보기 전 고정):
  종목   2021 이전 상장 56개(xs_data — 생존 편향 방어, BTC 포함) 1h → 4h, 2021-01 ~ 2026-08
  신호   **주력: 4h SuperTrend(20,4.0) 방향(T4 의 BTC 선택 그대로, 다시 고르지 않음)** — 상승 롱 · 하락 숏
         참고: 지난 30일(180봉) 수익률 부호 — 고전적 시계열 모멘텀. 판정에 안 쓴다.
  비중   동일가중 1/56, 총 1배. 봉 t 종가의 방향으로 t→t+1 보유. 뒤집힐 때만 거래.
  비용   taker 5bp × 거래 명목 · 실제 펀딩(정산 시각이 (t, t+1] 에 들면 포지션×요율)
  귀무   포지션 행렬을 **시간축으로 통째로 원형 이동**(1,000회, 이동 ≥ 30일). 회전율·롱숏 비율·보유 길이·
         코인 간 동조가 다 보존되고 '신호와 가격의 시점 정렬'만 깨진다 = 타이밍 정보만 제거.
  통과   ① 전체 기간 순수익 > 귀무 p95  ② 세 하위 구간 중 2개 이상 순수익 > 0.  둘 다.

    python3 -u -m research.exp_TP_trend_portfolio
"""
from __future__ import annotations

import sqlite3
import sys

import numpy as np

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from engine import candle_store as cs                 # noqa: E402
from engine import indicators as ind                  # noqa: E402
from engine.candles import Candles, resample          # noqa: E402

FEE = 0.0005
ST = (20, 4.0)
LOOKBACK = 180            # 30일 × 6봉
SAMPLES = 1000
MIN_SHIFT = 180
BAR_MS = 4 * 3_600_000


def load():
    conn = sqlite3.connect(cs.DB_PATH)
    syms = sorted(r[0][:-3] for r in conn.execute(
        "SELECT DISTINCT symbol FROM candle WHERE symbol LIKE '%\\_1H' ESCAPE '\\'"))
    H, L, C, T, F = [], [], [], None, []
    for s in syms:
        d = np.array(conn.execute("SELECT open_time,open,high,low,close,volume FROM candle WHERE symbol=? "
                                  "ORDER BY open_time", (s + "_1H",)).fetchall(), dtype=float)
        b = resample(Candles(d[:, 0].astype(np.int64), d[:, 1], d[:, 2], d[:, 3], d[:, 4], d[:, 5],
                             timeframe_min=60), 240)
        if T is None:
            T = b.open_time
        assert np.array_equal(T, b.open_time), f"{s} 시각 불일치"
        H.append(b.high); L.append(b.low); C.append(b.close)
        fs = cs.funding_schedule(s, int(T[0]), int(T[-1]) + BAR_MS)
        f = np.zeros(len(T))
        if fs:
            ft = np.array(list(fs.keys()), dtype=np.int64)
            fr = np.array(list(fs.values()))
            # 봉 t 종가(T+4h)에서 잡은 포지션이 (T+4h, T+8h] 정산을 맞는다 → 그 봉 다음 칸에 귀속
            k = (ft - T[0] - 1) // BAR_MS          # 정산이 속한 봉(열린 시각 기준 (T, T+4h])
            ok = (k >= 0) & (k < len(T))
            np.add.at(f, k[ok], fr[ok])
        F.append(f)
    return syms, T, np.array(H), np.array(L), np.array(C), np.array(F)


def signals(H, L, C):
    st = np.zeros_like(C)
    for i in range(len(C)):
        _, d = ind.supertrend(H[i], L[i], C[i], *ST)
        st[i] = np.nan_to_num(d)
    mom = np.zeros_like(C)
    mom[:, LOOKBACK:] = np.sign(C[:, LOOKBACK:] / C[:, :-LOOKBACK] - 1.0)
    return st, mom


def portfolio(pos, C, F, fee=FEE):
    """pos[i,t] = 봉 t 종가에서 잡은 방향 → 봉 t+1 수익. 반환: 봉별 포트폴리오 순수익(길이 n-1), 회전율."""
    n = C.shape[0]
    r = C[:, 1:] / C[:, :-1] - 1.0
    p = pos[:, :-1]
    turn = np.abs(np.diff(np.concatenate((np.zeros((n, 1)), p), axis=1), axis=1))
    fund = p * F[:, 1:]                                    # 롱이면 양(+) 요율을 낸다
    pr = (p * r - turn * fee - fund).sum(axis=0) / n
    return pr, turn.sum() / n


def stats(pr):
    eq = np.cumprod(1 + pr)
    yrs = len(pr) / (6 * 365)
    tot = (eq[-1] - 1) * 100
    ann = (eq[-1] ** (1 / yrs) - 1) * 100
    sh = pr.mean() / pr.std() * np.sqrt(6 * 365) if pr.std() > 0 else 0.0
    mdd = (1 - eq / np.maximum.accumulate(eq)).max() * 100
    return tot, ann, sh, mdd


def main() -> int:
    syms, T, H, L, C, F = load()
    st, mom = signals(H, L, C)
    import datetime as dt
    ym = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m")
    n = len(T)
    cuts = [0, n // 3, 2 * n // 3, n - 1]
    print(f"TP · 추세추종 포트폴리오 · {len(syms)}코인 동일가중 · 4h · {ym(T[0])}~{ym(T[-1])} · 귀무 원형이동 {SAMPLES}회")
    bh = (C[:, 1:] / C[:, :-1] - 1).mean(axis=0)
    t, a, s, m = stats(bh)
    print(f"  기준: 동일가중 보유(리밸런스) {t:+.1f}% · 연 {a:+.1f}% · 샤프 {s:.2f} · MDD {m:.0f}%")
    rng = np.random.default_rng(0)
    for name, pos, main_ in (("ST(20,4.0) 방향 [주력]", st, True), ("30일 모멘텀 부호 [참고]", mom, False)):
        print(f"\n■ {name}")
        for lab, fee, fund in (("gross", 0.0, False), ("net", FEE, True)):
            pr, turn = portfolio(pos, C, F if fund else np.zeros_like(F), fee)
            t, a, s, m = stats(pr)
            seg = []
            for i in range(3):
                tt, *_ = stats(pr[cuts[i]:cuts[i + 1]])
                seg.append(tt)
            print(f"  {lab:5} 총 {t:+8.1f}% · 연 {a:+6.1f}% · 샤프 {s:5.2f} · MDD {m:4.0f}% · 연회전 {turn / (n / 2190):.0f}배 | "
                  f"구간 {ym(T[cuts[0]])}~ {seg[0]:+.0f}% · {ym(T[cuts[1]])}~ {seg[1]:+.0f}% · {ym(T[cuts[2]])}~ {seg[2]:+.0f}%")
            if lab == "net":
                null = np.empty(SAMPLES)
                for k in range(SAMPLES):
                    sh = int(rng.integers(MIN_SHIFT, n - MIN_SHIFT))
                    null[k] = stats(portfolio(np.roll(pos, sh, axis=1), C, F, fee)[0])[0]
                p95 = np.percentile(null, 95)
                pct = (null < t).mean() * 100
                ok1 = t > p95
                ok2 = sum(x > 0 for x in seg) >= 2
                print(f"  귀무(시점만 어긋나게) 중앙 {np.median(null):+.1f}% · p95 {p95:+.1f}% · 백분위 {pct:.0f}")
                if main_:
                    print(f"  판정: ① 귀무 p95 초과 {'✅' if ok1 else '❌'} · ② 구간 양수 {sum(x > 0 for x in seg)}/3 "
                          f"{'✅' if ok2 else '❌'} → {'✅ 통과' if ok1 and ok2 else '❌ 기각'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
