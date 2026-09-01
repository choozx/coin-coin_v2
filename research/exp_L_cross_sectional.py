"""L — 횡단면(cross-sectional) 랭킹: 순위 정보에 엣지가 있는가.

가설. 방향성 예측은 여섯 번 막혔다(C·A2·B·F·G·K). 공통 원인은 'BTC 우상향 드리프트라는
귀무를 못 넘는다'였다. 횡단면은 개별 심볼의 방향을 맞히지 않는다 — 여러 심볼의 **순위**로
상위를 롱·하위를 숏 한다. 달러 중립이라 **드리프트 귀무와 애초에 경쟁하지 않는다.**

★ 이 실험이 답하는 질문은 하나다: **순위 정보에 값이 있는가.**
   그래서 귀무모델도 '랜덤 진입'이 아니라 **'랜덤 선택'** 이다 — 같은 시각에 같은 개수를
   롱숏하되 **어느 심볼을 고를지만** 무작위. 전략이 이걸 못 넘으면 순위는 정보가 아니다.

★★ 반드시 **두 단계**로 본다. 처음엔 순비용 기준으로만 재다가 크게 속았다:
   순위 전략은 랭킹이 이어져 회전율이 9~48% 인데 랜덤 선택은 매번 새로 뽑아 ~80% 다.
   그래서 순비용으로 재면 전략이 **덜 사고팔았다는 이유만으로** 귀무를 이긴다 —
   실제로 70조합 중 16개가 세 구간을 '통과' 했는데 **전부 -9%~-97% 손실**이었다
   (귀무가 -74%~-99% 라 이겼을 뿐). 비용 차이를 신호로 오독한 것이다.

   ① gross(비용 0): 순위가 랜덤 선택보다 나은가 = **신호가 있는가**
   ② net(실비용): 그 신호가 비용을 견디는가
   ①이 실패하면 ②는 볼 필요가 없다 — 없는 신호는 비용을 뺀들 생기지 않는다.

정직하게 설계한 것:
  · 유니버스는 상장 시점으로 고정(2021 이전 56개) — 생존 편향(xs_data.py 주석 참고)
  · 비용은 **실측 회전율**로 매긴다. 연속 리밸런싱에서 같은 심볼이 남으면 회전율이 낮다 —
    100% 로 가정하면 부당하게 죽이고, 0 으로 두면 부당하게 살린다.
  · 격자를 훑으므로 **다중검정**을 함께 본다. 70 조합을 p95 로 재면 우연히 3.5개가 통과한다.
    '몇 개가 통과했나'가 그 기대치를 넘는지가 진짜 판정이다.
  · **세 독립 구간**에서 일관되게 통과해야 한다(한 구간이면 커브핏 — 재개 조건 1번).

    python3 -m research.exp_L_cross_sectional
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone

import numpy as np

from engine import candle_store as cs

HOUR = 3_600_000
TAKER = 0.0005                 # 편도 5bp (USDT 무기한 VIP0)
SAMPLES = 2000                 # 귀무 표본
SEGMENTS = (("2021-22", "2021-01-01", "2023-01-01"),
            ("2023-24", "2023-01-01", "2025-01-01"),
            ("2025-26", "2025-01-01", "2027-01-01"))
LOOKBACKS = (6, 12, 24, 72, 168, 336, 720)     # 시간 단위: 6h~30d
HOLDS = (6, 12, 24, 72, 168)                   # 6h~7d
KS = (5, 11)                                   # 상위/하위 k개 (56개 중 ~9%/~20%)


def load_matrix():
    """(심볼목록, 시각배열, 종가행렬 [S×T]). 전 심볼 봉수가 같다는 전제(수집기가 보장)."""
    conn = sqlite3.connect(cs.DB_PATH)
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM candle WHERE symbol LIKE '%\\_1H' ESCAPE '\\' ORDER BY symbol")]
    if not syms:
        raise SystemExit("수집된 1H 데이터가 없다 — python3 -m research.xs_data 먼저")
    mats, times = [], None
    for s in syms:
        rows = conn.execute("SELECT open_time, close FROM candle WHERE symbol=? ORDER BY open_time",
                            (s,)).fetchall()
        t = np.array([r[0] for r in rows], dtype=np.int64)
        if times is None:
            times = t
        elif len(t) != len(times) or t[0] != times[0]:
            raise SystemExit(f"{s}: 시각 축이 다르다 — 수집을 다시 할 것")
        mats.append(np.array([r[1] for r in rows], dtype=np.float64))
    conn.close()
    return [s[:-3] for s in syms], times, np.vstack(mats)


def _steps(px, lb, hold):
    """리밸런싱 시점들과 각 시점의 전방수익률 [n×S] — 전략·귀무가 **같은 시점**을 쓰게."""
    T = px.shape[1]
    ts = list(range(lb, T - hold, hold))
    if not ts:
        return [], None
    fwd = np.array([px[:, t + hold] / px[:, t] - 1.0 for t in ts])
    return ts, fwd


def simulate(px, lb, hold, k):
    """전략: 룩백 수익률 상위 k 롱 / 하위 k 숏. 반환 (총수익률%, 평균회전율).

    비용 = 4 × 회전율 × 편도수수료 (롱·숏 각 1 명목 × 청산+진입 2 레그).
    회전율은 직전 집합과의 실제 겹침으로 잰다 — 100% 가정은 부당하게 죽이고 0 은 부당하게 살린다.
    """
    ts, fwd = _steps(px, lb, hold)
    if not ts:
        return 0.0, 0.0
    eq, turns = 1.0, []
    prev_l = prev_s = None
    for i, t in enumerate(ts):
        sig = px[:, t] / px[:, t - lb] - 1.0
        if not np.isfinite(sig).all() or not np.isfinite(fwd[i]).all():
            continue
        order = np.argsort(sig)
        short_i, long_i = order[:k], order[-k:]
        ret = fwd[i][long_i].mean() - fwd[i][short_i].mean()
        if prev_l is None:
            turn = 1.0
        else:
            keep = (len(np.intersect1d(long_i, prev_l)) +
                    len(np.intersect1d(short_i, prev_s))) / (2 * k)
            turn = 1.0 - keep
        turns.append(turn)
        eq = max(eq * (1.0 + ret - 4.0 * turn * TAKER), 1e-9)
        prev_l, prev_s = long_i, short_i
    return (eq - 1.0) * 100.0, float(np.mean(turns)) if turns else 0.0


def null_dist(px, lb, hold, k, samples=SAMPLES, seed=0):
    """랜덤 '선택' 귀무 — 같은 시점·같은 개수를 무작위로 고른다. **순위 정보만** 제거된다.

    전 표본을 한꺼번에 굴린다(벡터화). 회전율도 표본별로 실제 겹침을 세므로 전략과 같은
    기준으로 비용을 문다 — 귀무만 싸게 하면 부당하게 이긴다(F 의 교훈).
    """
    ts, fwd = _steps(px, lb, hold)
    if not ts:
        return np.zeros(1)
    S = px.shape[1] and px.shape[0]
    rng = np.random.default_rng(seed)
    eq = np.ones(samples)
    rows = np.arange(samples)[:, None]
    prevL = prevS = None
    for i in range(len(ts)):
        pick = np.argsort(rng.random((samples, S)), axis=1)[:, :2 * k]
        li, si = pick[:, :k], pick[:, k:]
        f = fwd[i]
        ret = f[li].mean(axis=1) - f[si].mean(axis=1)
        mL = np.zeros((samples, S), dtype=bool); mL[rows, li] = True
        mS = np.zeros((samples, S), dtype=bool); mS[rows, si] = True
        if prevL is None:
            turn = np.ones(samples)
        else:
            keep = ((mL & prevL).sum(1) + (mS & prevS).sum(1)) / (2.0 * k)
            turn = 1.0 - keep
        eq = np.maximum(eq * (1.0 + ret - 4.0 * turn * TAKER), 1e-9)
        prevL, prevS = mL, mS
    return (eq - 1.0) * 100.0


def scan(px, times, taker):
    """격자 × 구간 스캔 → {구간: [(lb,h,k,전략%,p95%)]} 통과 목록. taker 는 전략·귀무 공통."""
    # ★ globals() 로 바꾼다. `python3 -m` 으로 돌리면 이 모듈은 __main__ 이라
    #   `import research.exp_L_cross_sectional` 은 **두 번째 사본**을 만든다 — 거기 TAKER 를
    #   바꿔도 지금 도는 simulate/null_dist 의 전역은 안 바뀐다(실제로 이걸로 속았다:
    #   gross 스캔이 net 과 똑같은 숫자를 냈다).
    g = globals()
    old, g["TAKER"] = g["TAKER"], taker
    grid = [(lb, h, k) for lb in LOOKBACKS for h in HOLDS for k in KS]
    out = {}
    try:
        for name, a, b in SEGMENTS:
            lo = int(datetime.fromisoformat(a).replace(tzinfo=timezone.utc).timestamp() * 1000)
            hi = int(datetime.fromisoformat(b).replace(tzinfo=timezone.utc).timestamp() * 1000)
            seg = px[:, (times >= lo) & (times < hi)]
            hits, nulls = [], {}
            for lb, h, k in grid:
                if seg.shape[1] <= lb + h + 1:
                    continue
                r, _ = simulate(seg, lb, h, k)
                if (lb, h, k) not in nulls:
                    nulls[(lb, h, k)] = null_dist(seg, lb, h, k)
                p95 = float(np.percentile(nulls[(lb, h, k)], 95))
                if r > p95:
                    hits.append((lb, h, k, r, p95))
            out[name] = hits
            print(f"   {name}: 통과 {len(hits):2d}/{len(grid)} (우연 기대 {len(grid)*0.05:.1f})", flush=True)
    finally:
        g["TAKER"] = old
    return out, len(grid)


def common_of(hits):
    sets = [{(lb, h, k) for lb, h, k, _, _ in v} for v in hits.values()]
    return set.intersection(*sets) if sets and all(sets) else set()


def main():
    syms, times, px = load_matrix()
    f = lambda t: datetime.fromtimestamp(t / 1000, timezone.utc)
    print(f"L · 횡단면 · {len(syms)}심볼 × {px.shape[1]:,}봉(1h) · "
          f"{f(times[0]):%Y-%m-%d} ~ {f(times[-1]):%Y-%m-%d}")
    print(f"  귀무 = 랜덤 '선택'(같은 개수·같은 시각, 심볼만 무작위)\n")

    print("① gross (비용 0) — 순위에 신호가 있는가")
    g, n = scan(px, times, 0.0)
    gc = common_of(g)
    print(f"   ★ 세 구간 공통 통과 {len(gc)}개 " + (f"→ {sorted(gc)[:5]}" if gc else ""))

    if not gc:
        print("\n판정: ❌ 기각 — 비용을 0으로 둬도 순위가 랜덤 선택을 못 넘는다.")
        print("      신호가 없으므로 net 단계는 볼 필요가 없다(없는 신호는 비용을 뺀들 안 생긴다).")
        return

    print(f"\n② net (편도 {TAKER*100:.3f}%) — 그 신호가 비용을 견디는가")
    nt, _ = scan(px, times, TAKER)
    nc = common_of(nt) & gc
    print(f"   ★ gross·net 모두 세 구간 통과 {len(nc)}개")
    print("\n판정: " + ("✅ 후보 발견 — 재개 조건 2·3 검사로" if nc else "❌ 기각 — 신호는 있으나 비용을 못 견딘다"))


if __name__ == "__main__":
    main()
