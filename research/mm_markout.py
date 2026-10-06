"""Q 1단계 — markout 분석. 입력은 research/mm_collect.py 가 받아 적은 data/mm/<run>/.

질문 하나: **최우선 호가에 걸려 있다가 체결된 쪽(maker)은 체결 뒤 돈을 벌었나?**
내 주문이 없어도 '그 자리에 있던 누군가'의 체결로 재구성할 수 있다.

체결 하나(aggTrade)마다 maker 방향 s = +1(매수 호가가 맞음, m=1) / −1(매도 호가가 맞음):
    mid₀    = 체결 직전(엄격히 이전 시각) 최우선 호가의 중간값
    반스프레드  hs   = s·(mid₀ − p)               ← 걸어둔 값으로 버는 몫
    markout(h)    = s·(mid(T+h) − p)          ← h 뒤 중간값으로 평가한 손익
    역선택   AS(h)  = hs − markout(h) = −s·(mid(T+h) − mid₀)
    순손익(h)      = markout(h) − maker 수수료  (USDC 0bp · USDT 2bp)
모두 bp(체결가 대비).

★ 정직하게 볼 것 두 가지
  ① **최우선(touch) 체결만** 센다 — p 가 직전 최우선가와 같은 것. 더 깊은 호가의 체결은 우리가 걸
    자리가 아니다.
  ② **줄 맨 뒤 효과.** 새로 건 주문은 대기열 맨 뒤라, 그 가격대가 **통째로 쓸려 나갈 때** 주로 체결된다
    — 그게 가장 나쁜 체결이다. 그래서 '쓸림 체결'(체결 50ms 뒤 그 가격대가 사라짐)을 따로 낸다.
    전체 평균은 낙관 쪽, 쓸림 체결은 비관 쪽 경계다. 실제는 그 사이.
  불확실성은 1분 블록 평균들의 표준오차로 낸다(체결은 몰려 나와 서로 독립이 아니다).

    python3 -u -m research.mm_markout [data/mm/<run>]
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np

HS = (1_000, 5_000, 30_000, 60_000, 300_000)          # ms
SWEEP_MS = 50
ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "mm")


def maker_fee_bp(sym: str) -> float:
    return 0.0 if sym.endswith("USDC") else 2.0


def load_csv(path: str) -> np.ndarray:
    try:
        a = np.loadtxt(path, delimiter=",", skiprows=1, ndmin=2)
    except (ValueError, StopIteration):
        # 수집 중단으로 마지막 줄이 잘렸을 수 있다 — 잘린 줄만 버린다
        with open(path) as f:
            lines = f.read().splitlines()[1:]
        ok = [ln for ln in lines if ln.count(",") == lines[0].count(",") and not ln.endswith(",")]
        a = np.array([[float(x) for x in ln.split(",")] for ln in ok[:-1]]) if len(ok) > 1 else np.zeros((0, 5))
    return a


def block_se(t_ms: np.ndarray, x: np.ndarray, block_ms: int = 60_000):
    """1분 블록 평균의 평균과 표준오차."""
    if len(x) == 0:
        return np.nan, np.nan, 0
    b = (t_ms // block_ms).astype(np.int64)
    u, inv = np.unique(b, return_inverse=True)
    sums = np.bincount(inv, weights=x)
    cnt = np.bincount(inv)
    m = sums / cnt
    if len(m) < 2:
        return float(m.mean()), np.nan, len(m)
    return float(x.mean()), float(m.std(ddof=1) / np.sqrt(len(m))), len(m)


def analyze(run: str, sym: str, gaps: np.ndarray):
    bk = load_csv(os.path.join(run, f"{sym}_book.csv"))
    tr = load_csv(os.path.join(run, f"{sym}_trade.csv"))
    if len(bk) < 100 or len(tr) < 100:
        return None
    o = np.argsort(bk[:, 0], kind="stable")
    bk = bk[o]
    bt, bid, bq, ask, aq = bk.T
    mid = (bid + ask) / 2
    t, p, q, m = tr.T
    s = np.where(m == 1, 1.0, -1.0)

    i0 = np.searchsorted(bt, t, side="left") - 1          # 엄격히 이전 호가
    ok = i0 >= 0
    t_end = bt[-1] - max(HS)
    ok &= t <= t_end
    for d0, d1 in gaps:                                    # 끊긴 구간 근처 버림
        ok &= ~((t >= d0 - max(HS)) & (t <= d1 + 1_000))
    i0c = np.clip(i0, 0, None)
    tick = np.min(np.diff(np.unique(np.concatenate((bid, ask))))) if len(bid) > 2 else np.nan
    touch = np.where(s > 0, np.abs(p - bid[i0c]) < tick / 2, np.abs(p - ask[i0c]) < tick / 2)
    sel = ok & touch
    t, p, q, s, i0 = t[sel], p[sel], q[sel], s[sel], i0c[sel]
    if len(t) < 50:
        return None
    mid0 = mid[i0]
    hs = s * (mid0 - p) / p * 1e4
    mk = {}
    for h in HS:
        j = np.searchsorted(bt, t + h, side="right") - 1
        mk[h] = s * (mid[j] - p) / p * 1e4
    j = np.searchsorted(bt, t + SWEEP_MS, side="right") - 1
    swept = np.where(s > 0, bid[j] < p - tick / 2, ask[j] > p + tick / 2)

    dur_h = (bt[-1] - bt[0]) / 3_600_000
    queue_usd = np.median(np.where(True, (bq + aq) / 2 * mid, 0))
    fill_usd_h = (p * q).sum() / dur_h
    return {
        "sym": sym, "fee": maker_fee_bp(sym), "n": len(t), "hours": dur_h,
        "spread_bp": float(np.median((ask - bid) / mid * 1e4)), "tick_bp": float(tick / np.median(mid) * 1e4),
        "hs": float(hs.mean()), "mk": {h: block_se(t, mk[h]) for h in HS},
        "mk_sw": {h: block_se(t[swept], mk[h][swept]) for h in HS},
        "mk_ns": {h: block_se(t[~swept], mk[h][~swept]) for h in HS},
        "swept": float(swept.mean()), "queue_usd": float(queue_usd), "fill_usd_h": float(fill_usd_h),
        # 줄 회전 시간: 최우선 대기 물량을 그 자리 체결이 다 먹는 데 걸리는 시간(초)
        "turn_s": float(queue_usd / (fill_usd_h / 2 / 3600)) if fill_usd_h > 0 else np.nan,
    }


def main() -> int:
    run = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob(os.path.join(ROOT, "*")))[-1]
    g = os.path.join(run, "gaps.csv")
    gaps = np.zeros((0, 2))
    if os.path.exists(g):
        a = np.genfromtxt(g, delimiter=",", skip_header=1, usecols=(1, 2), ndmin=2)
        gaps = a[~np.isnan(a).any(axis=1)] if a.size else gaps
    syms = sorted({os.path.basename(f)[:-9] for f in glob.glob(os.path.join(run, "*_book.csv"))})
    res = [r for r in (analyze(run, s, gaps) for s in syms) if r]
    res.sort(key=lambda r: -(r["mk"][30_000][0] - r["fee"]))
    print(f"markout · {os.path.basename(run)} · 끊김 {len(gaps)}회 · 최우선(touch) 체결만 · 단위 bp")
    print(f"  순손익 = markout − maker수수료(USDC 0 · USDT 2).  ±는 1분 블록 표준오차.  "
          f"'쓸림'= 체결 50ms 뒤 그 가격대가 사라진 체결(줄 맨 뒤 = 비관 경계)\n")
    hdr = (f"  {'심볼':13} {'시간':>4} {'체결':>7} {'스프레드':>6} {'반스프':>6} "
           + " ".join(f"{'mk'+str(h//1000)+'s':>12}" for h in HS)
           + f" {'순(30s)':>8} {'쓸림%':>5} {'쓸림mk30s':>11} {'줄회전':>7}")
    print(hdr)
    for r in res:
        cells = " ".join(f"{r['mk'][h][0]:+6.2f}±{r['mk'][h][1]:4.2f}" for h in HS)
        net = r["mk"][30_000][0] - r["fee"]
        sw = r["mk_sw"][30_000]
        print(f"  {r['sym']:13} {r['hours']:4.1f} {r['n']:7d} {r['spread_bp']:6.2f} {r['hs']:6.2f} {cells} "
              f"{net:+8.2f} {r['swept']*100:5.0f} {sw[0]:+6.2f}±{sw[1]:4.2f} {r['turn_s']:6.0f}s")
    print("\n  판정(사전 고정): 순(30s)·순(60s) 이 둘 다 > 2×표준오차 로 양수인 심볼만 '후보'.\n"
          "  쓸림 체결 markout 까지 양수면 강한 후보(줄 맨 뒤에서도 번다).")
    cands = []
    for r in res:
        a30, e30, _ = r["mk"][30_000]
        a60, e60, _ = r["mk"][60_000]
        if a30 - r["fee"] > 2 * e30 and a60 - r["fee"] > 2 * e60:
            strong = r["mk_sw"][30_000][0] - r["fee"] > 0
            cands.append(r["sym"] + (" (강)" if strong else ""))
    print(f"  후보: {', '.join(cands) if cands else '없음'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
