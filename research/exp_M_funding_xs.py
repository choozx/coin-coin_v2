"""M — 펀딩률 횡단면: 펀딩이 높은 퍼프를 숏, 낮은 퍼프를 롱.

가설. 지금까지 **구조적으로 양성이었던 유일한 것이 펀딩**이다(H: BTC 연 +11.7%, 양수 85.5%).
지표에서 짜낸 패턴이 아니라 **누군가 실제로 내는 현금**이라서다. H·I·J 가 죽은 건 신호가 없어서가
아니라 운영 구조 때문이었다 — 현물 다리가 필요했고(봇은 선물 전용), 강세장에 숏 증거금이 녹아
리밸런싱이 포지션을 1/3 로 깎았다(J: +6.72% → +4.1%).

**선물끼리 롱숏하면 그 둘을 동시에 피한다.** 현물 다리가 없고, 양다리 손익이 서로 상쇄된다.
  펀딩 최저 퍼프를 롱 (음수면 받는다) · 펀딩 최고 퍼프를 숏 (양수면 받는다)

L 과의 차이: L 은 **과거 수익률**로 순위를 매겼다(순수 가격 신호 — 없었다). M 은 **펀딩률**,
즉 포지션 쏠림이 만든 현금흐름으로 매긴다. 범주가 다르다.

정직한 사전 확률: **중간.** 이건 실제 데스크가 굴리는 전략이라 공짜일 리 없다. 펀딩이 높다는 건
롱이 몰렸다는 뜻이고, 그걸 숏하면 가격이 계속 오를 때 얻어맞는다. **펀딩 수취가 가격 손실로
상쇄되는지**가 정확히 판정 대상이다. 실측 스프레드는 상위10%−하위10% 중앙 연 23.9%.

판정은 L 의 교훈대로 (research/README.md 판정 원칙):
  ① gross(거래수수료 0) — 펀딩 순위에 정보가 있는가. 귀무는 랜덤 '선택'.
  ② net(실수수료) — 그 정보가 비용을 견디는가.
  ★ 통과가 나오면 **절대 수익률을 반드시 확인**한다(둘 다 파산이면 엣지가 아니다).

    python3 -u -m research.exp_M_funding_xs
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import numpy as np

from engine import candle_store as cs

TAKER = 0.0005
SAMPLES = 2000
FUND_MS = 8 * 3_600_000
SEGMENTS = (("2021-22", "2021-01-01", "2023-01-01"),
            ("2023-24", "2023-01-01", "2025-01-01"),
            ("2025-26", "2025-01-01", "2027-01-01"))
LOOKBACKS = (1, 3, 9, 21)        # 펀딩 주기 수(8h·1d·3d·7d) 평균으로 순위
HOLDS = (1, 3, 9, 21)
KS = (5, 11)


def load():
    """(심볼, 펀딩시각, 가격행렬 P[S×N], 펀딩행렬 F[S×N]) — 펀딩 시각 격자에 맞춘다."""
    conn = sqlite3.connect(cs.DB_PATH)
    syms = sorted(r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM candle WHERE symbol LIKE '%\\_1H' ESCAPE '\\'"))
    syms = [s[:-3] for s in syms]
    fund = {}
    for s, t, r in conn.execute("SELECT symbol, funding_time, rate FROM funding"):
        fund.setdefault(s, {})[int(t) // FUND_MS * FUND_MS] = float(r)
    px = {}
    for s in syms:
        rows = conn.execute("SELECT open_time, close FROM candle WHERE symbol=? ORDER BY open_time",
                            (s + "_1H",)).fetchall()
        px[s] = {int(t): float(c) for t, c in rows}
    conn.close()
    # 모든 심볼이 펀딩·가격을 함께 갖는 시각만 쓴다(결측 있으면 순위가 왜곡된다)
    syms = [s for s in syms if s in fund and len(fund[s]) > 1000]
    times = sorted(set.intersection(*[set(fund[s]) for s in syms]))
    times = [t for t in times if all(t in px[s] for s in syms)]
    P = np.array([[px[s][t] for t in times] for s in syms])
    F = np.array([[fund[s][t] for t in times] for s in syms])
    return syms, np.array(times, dtype=np.int64), P, F


def simulate(P, F, lb, hold, k, taker=TAKER):
    """전략: 최근 lb 주기 평균 펀딩 하위 k 롱 / 상위 k 숏, hold 주기 보유.

    수익 = 가격손익(롱-숏) + 펀딩수취(숏의 펀딩 - 롱의 펀딩, 보유기간 합) - 거래비용.
    """
    S, N = P.shape
    eq, turns, fsum, psum = 1.0, [], 0.0, 0.0
    prevL = prevS = None
    for t in range(lb, N - hold - 1, hold):
        sig = F[:, t - lb:t].mean(axis=1)
        order = np.argsort(sig)
        long_i, short_i = order[:k], order[-k:]              # 낮은 펀딩 롱 / 높은 펀딩 숏
        pr = P[long_i, t + hold] / P[long_i, t] - 1.0
        sr = P[short_i, t + hold] / P[short_i, t] - 1.0
        price = pr.mean() - sr.mean()
        # ★ t 에 진입하면 t 에 정산되는 펀딩은 **못 받는다**(그 시점에 이미 보유 중이어야 한다).
        #   t+1 부터 센다 — 애매한 쪽을 우리에게 불리하게 잡는다.
        fund = (F[short_i, t + 1:t + hold + 1].sum(axis=1).mean()
                - F[long_i, t + 1:t + hold + 1].sum(axis=1).mean())
        turn = 1.0 if prevL is None else 1.0 - (
            len(np.intersect1d(long_i, prevL)) + len(np.intersect1d(short_i, prevS))) / (2 * k)
        turns.append(turn); fsum += fund; psum += price
        eq = max(eq * (1.0 + price + fund - 4.0 * turn * taker), 1e-9)
        prevL, prevS = long_i, short_i
    return ((eq - 1.0) * 100.0, float(np.mean(turns)) if turns else 0.0,
            fsum * 100.0, psum * 100.0)


def null_dist(P, F, lb, hold, k, taker=TAKER, samples=SAMPLES, seed=0):
    """랜덤 '선택' 귀무 — 같은 시각·같은 개수를 무작위로. **펀딩 순위 정보만** 제거된다.

    귀무도 펀딩을 받는다(무작위 롱숏이라 기대 스프레드는 ~0). 비용도 같은 식으로 문다 —
    귀무만 싸게 하거나 펀딩을 안 주면 부당하게 이긴다.
    """
    S, N = P.shape
    rng = np.random.default_rng(seed)
    eq = np.ones(samples)
    rows = np.arange(samples)[:, None]
    prevL = prevS = None
    for t in range(lb, N - hold - 1, hold):
        pick = np.argsort(rng.random((samples, S)), axis=1)[:, :2 * k]
        li, si = pick[:, :k], pick[:, k:]
        gp = P[:, t + hold] / P[:, t] - 1.0
        gf = F[:, t + 1:t + hold + 1].sum(axis=1)          # 전략과 같은 규칙(t 정산분 제외)
        ret = gp[li].mean(1) - gp[si].mean(1) + gf[si].mean(1) - gf[li].mean(1)
        mL = np.zeros((samples, S), bool); mL[rows, li] = True
        mS = np.zeros((samples, S), bool); mS[rows, si] = True
        turn = np.ones(samples) if prevL is None else 1.0 - (
            (mL & prevL).sum(1) + (mS & prevS).sum(1)) / (2.0 * k)
        eq = np.maximum(eq * (1.0 + ret - 4.0 * turn * taker), 1e-9)
        prevL, prevS = mL, mS
    return (eq - 1.0) * 100.0


def scan(P, F, times, taker, label):
    grid = [(lb, h, k) for lb in LOOKBACKS for h in HOLDS for k in KS]
    print(f"\n{label}")
    hits_all = {}
    for name, a, b in SEGMENTS:
        lo = int(datetime.fromisoformat(a).replace(tzinfo=timezone.utc).timestamp() * 1000)
        hi = int(datetime.fromisoformat(b).replace(tzinfo=timezone.utc).timestamp() * 1000)
        m = (times >= lo) & (times < hi)
        Ps, Fs = P[:, m], F[:, m]
        hits, best = [], None
        for lb, h, k in grid:
            if Ps.shape[1] <= lb + h + 1:
                continue
            r, turn, fs, ps = simulate(Ps, Fs, lb, h, k, taker)
            p95 = float(np.percentile(null_dist(Ps, Fs, lb, h, k, taker), 95))
            if r > p95:
                hits.append((lb, h, k, r, p95))
            if best is None or (r - p95) > best[0]:
                best = (r - p95, lb, h, k, r, p95, turn, fs, ps)
        hits_all[name] = hits
        print(f"   {name}: 통과 {len(hits):2d}/{len(grid)} (우연 기대 {len(grid)*0.05:.1f})", flush=True)
        if best:
            _, lb, h, k, r, p95, turn, fs, ps = best
            print(f"      최대초과: lb={lb} hold={h} k={k} → 전략 {r:+.1f}% vs p95 {p95:+.1f}% "
                  f"(펀딩 {fs:+.0f}%p · 가격 {ps:+.0f}%p · 회전 {turn:.0%})")
    sets = [{(a, b, c) for a, b, c, _, _ in v} for v in hits_all.values()]
    common = set.intersection(*sets) if sets and all(sets) else set()
    print(f"   ★ 세 구간 공통 통과 {len(common)}개" + (f" → {sorted(common)[:6]}" if common else ""))
    return common


def main():
    syms, times, P, F = load()
    f = lambda t: datetime.fromtimestamp(t / 1000, timezone.utc)
    print(f"M · 펀딩 횡단면 · {len(syms)}심볼 × {len(times):,}개 펀딩시각(8h) · "
          f"{f(times[0]):%Y-%m-%d} ~ {f(times[-1]):%Y-%m-%d}")
    print(f"  롱=펀딩 최저 k · 숏=펀딩 최고 k · 귀무=랜덤 선택(펀딩도 같이 받음)")

    gc = scan(P, F, times, 0.0, "① gross (거래수수료 0) — 펀딩 순위에 정보가 있는가")
    if not gc:
        print("\n판정: ❌ 기각 — 수수료를 0으로 둬도 펀딩 순위가 랜덤 선택을 못 넘는다.")
        return
    nc = scan(P, F, times, TAKER, f"② net (편도 {TAKER*100:.3f}%) — 비용을 견디는가") & gc
    print(f"\n★ gross·net 모두 세 구간 통과: {len(nc)}개 {sorted(nc) if nc else ''}")
    print("판정: " + ("✅ 후보 — 절대 수익률 확인 후 재개 조건 2·3 검사로"
                    if nc else "❌ 기각 — 신호는 있으나 비용을 못 견딘다"))


if __name__ == "__main__":
    main()
