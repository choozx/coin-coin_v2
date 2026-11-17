"""M 재검증 — 연구가 끝난 뒤 쌓인 데이터(2026-09-01 이후)로, 설정을 바꾸지 않고 한 번만.

M 연구(exp_M_funding_xs)는 2026-09-01 까지의 데이터로 했다. 그 뒤 구간은 아무도 안 봤다 —
섀도 운용 없이 얻을 수 있는 진짜 out-of-sample 이다. **결과를 보고 설정을 고르지 않는다.**

고정 설정(BACKLOG M ③ '상위 30·k=7', 재구성한 설정을 여기서 확정):
  유니버스 = 각 시각 직전 168h 거래대금 상위 30 (2021 이전 상장 56심볼 중)
  신호     = 직전 21회 정산(7일) 평균 펀딩 · 하위 7 롱 / 상위 7 숏, 동일 명목
  보유     = 3회 정산(24h)마다 재선정 · 비용 = 편도 5 / 15 / 30bp

판정(사전에 정함):
  ✅ 15bp 에서 절대 수익 > 0 이고 펀딩 수취가 비용을 넘는다 — 세 시작 위상 모두.
  귀무 p95 초과는 참고만 한다: 11주는 짧아 랜덤 선택의 분포가 넓다(넘기 어렵다는 걸 알고 본다).

    python3 -u -m research.exp_M_oos
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

import numpy as np

from research.exp_M_funding_xs import load, null_dist, simulate, universe_mask

TOP, K, LB, H = 30, 7, 21, 3
COSTS = (0.0005, 0.0015, 0.0030)
OOS_START = "2026-09-01T08:00:00"     # 연구 데이터의 마지막 공통 시각 다음 정산
REPRO = ("2025-01-01T00:00:00", OOS_START, "+43.9")   # BACKLOG ③ 수정 후 · 15bp


def _ms(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def _day(t):
    return datetime.fromtimestamp(int(t) / 1000, timezone.utc).strftime("%Y-%m-%d")


def mdd(curve):
    eq = np.r_[1.0, np.asarray(curve)]
    return float(((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min() * 100)


def main():
    syms, times, P, F, V = load(with_volume=True)
    U = universe_mask(V, TOP)
    print(f"M 재검증 · {len(syms)}심볼 · 펀딩시각 {len(times):,}개 · {_day(times[0])} ~ {_day(times[-1])}")
    print(f"고정 설정: 상위 {TOP} · k={K} · lb={LB}(7일) · hold={H}(24h)")

    # ── 0. 재현: 연구 때 숫자가 이 코드로 다시 나오는가 ─────────────────────────
    a, b, want = REPRO
    m = (times >= _ms(a)) & (times < _ms(b))
    r, turn, fs, ps = simulate(P[:, m], F[:, m], LB, H, K, 0.0015, U[:, m])
    print(f"\n0. 재현 (2025-01 ~ 2026-09-01, 15bp): {r:+.1f}%  (BACKLOG {want}%) "
          f"{'✅ 일치' if abs(r - float(want)) < 0.5 else '⚠ 불일치'}")

    # ── 1. OOS ──────────────────────────────────────────────────────────────
    i0 = int(np.searchsorted(times, _ms(OOS_START)))
    n_oos = len(times) - i0
    days = n_oos * 8 / 24
    print(f"\n1. OOS: {_day(times[i0])} ~ {_day(times[-1])} · 정산 {n_oos}회 ≈ {days:.0f}일")
    print("   (위상 = 첫 재선정을 0·8·16시간 중 언제로 하느냐. 하나만 보면 우연히 좋은 날을 고를 수 있어 셋 다 본다)")

    picks_L, picks_S = Counter(), Counter()
    for c in COSTS:
        print(f"\n   편도 {c*1e4:.0f}bp")
        for off in range(H):
            lo = i0 - LB + off                        # 첫 진입이 OOS 시작(+off) 에 오게 lb 만큼 앞에서 자른다
            Ps, Fs, Us = P[:, lo:], F[:, lo:], U[:, lo:]
            log = []
            r, turn, fs, ps = simulate(Ps, Fs, LB, H, K, c, Us, log=log)
            cost = 4 * c * sum(x[4] for x in log) * 100
            p95 = float(np.percentile(null_dist(Ps, Fs, LB, H, K, c, U=Us), 95))
            ann = ((1 + r / 100) ** (365 / (len(log) * H * 8 / 24)) - 1) * 100
            worst = min(x[2] for x in log) * 100
            ok = r > 0 and fs > cost
            print(f"     위상 {off}: {r:+6.2f}% (연 {ann:+6.1f}%) · 펀딩 {fs:+5.2f}%p · 가격 {ps:+6.2f}%p · "
                  f"비용 −{cost:4.2f}%p · 회전 {turn:3.0%} · MDD {mdd([x[1] for x in log]):+5.1f}% · "
                  f"최악 하루 가격 {worst:+5.2f}% · 귀무 p95 {p95:+5.1f}% {'✅' if ok else '❌'}", flush=True)
            if c == COSTS[1] and off == 0:
                for x in log:
                    picks_L.update(syms[i] for i in x[5]); picks_S.update(syms[i] for i in x[6])

    n = sum(picks_S.values()) / K
    print(f"\n2. 자주 고른 종목 (15bp·위상 0, 재선정 {n:.0f}회 중)")
    print("   숏(펀딩 높음): " + " · ".join(f"{s[:-4]} {v}" for s, v in picks_S.most_common(8)))
    print("   롱(펀딩 낮음): " + " · ".join(f"{s[:-4]} {v}" for s, v in picks_L.most_common(8)))

    # 같은 기간의 횡단면 펀딩 스프레드 — 연구 기간 중앙(연 23.9%)과 비교
    Fo = F[:, i0:]
    Uo = U[:, i0:]
    spread = []
    for t in range(Fo.shape[1]):
        f = np.sort(Fo[Uo[:, t], t])
        spread.append(f[-K:].mean() - f[:K].mean())
    print(f"\n3. 이 기간 상위 {K} − 하위 {K} 펀딩 차(그 시각 값): 중앙 {np.median(spread)*100:.4f}%/8h "
          f"= 연 {np.median(spread)*3*365*100:.1f}%")


if __name__ == "__main__":
    main()
