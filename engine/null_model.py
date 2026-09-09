"""귀무모델 — "이 성과가 **우연보다 나은가**".

이 프로젝트에서 실제로 결론을 뒤집어 온 판정 도구다. 백테스트 수익률만 보면 좋아 보이는
전략이 여럿 있었지만, 같은 횟수·같은 보유시간·같은 방향으로 **아무 근거 없이** 진입한
분포와 대면 대부분 그 아래였다(C·A2·B·F·G·K·L 기각). 그래서 여기 있는 건 '지표'가 아니라
**게이트**다.

지금까지 research/lib.py 에만 있었다. 최적화기(engine/optimize.py)는 IS/OOS 만 보고
있었는데, 그건 "다른 구간에서도 되나"를 물을 뿐 **"우연보다 나은가"** 를 안 묻는다.
그 차이가 실제로 사고를 냈다: 지금 라이브 프리셋은 이름부터 '최적화' 산물이고 IS/OOS 를
통과했지만, K 에서 귀무 p95(+59.35%)에 한참 못 미쳐(백분위 86) 기각됐다. 그래서 판정을
engine 으로 옮겨 두 경로가 **같은 구현**을 쓰게 한다(백테스트↔라이브에서 이미 배운 교훈).

★ 다중검정. 격자로 C 조합을 훑으면 우연히 p95 를 넘는 게 C×5% 개 나온다. 500조합이면
25개다. 문턱을 Bonferroni 로 올리면(99.99 백분위) 표본이 모자라 추정 자체가 불안정해진다.
대신 **격자 탐색이 실제로 하는 일을 그대로 흉내낸다**: 무근거 진입 C 개를 뽑아 그중
최고를 취하기를 반복해 **'C개 중 최고'의 분포**를 만들고, 우리 최고 조합을 거기에 댄다.
그게 "많이 찾아봐서 하나 걸린 것"과 "진짜"를 가르는 정직한 비교다.
"""
from __future__ import annotations

import numpy as np

from . import binance_math as bm
from .candles import TIMEFRAME_MINUTES, resample


def simulate(base, tf_min: int, n_trades: int, hold_bars: int, side: str,
             leverage: int = 1, size_fraction: float = 0.10, samples: int = 2000,
             seed: int = 0, taker_fee: float = None) -> np.ndarray:
    """랜덤 진입 몬테카를로 → 총수익률(%) 분포.

    모델(근사): 각 트레이드 = 상위TF 봉 무작위 진입 → hold_bars 뒤 청산.
      per-trade = side*lev*frac*(exit/entry-1) - 왕복 수수료, equity 복리.
    """
    c = resample(base, tf_min).close.astype(np.float64)
    n = len(c)
    if n <= hold_bars + 1 or n_trades <= 0:
        return np.zeros(1)
    taker = bm.DEFAULT_TAKER_FEE if taker_fee is None else taker_fee
    rt_fee = 2 * taker * leverage * size_fraction
    sgn = 1.0 if side == "long" else -1.0
    rng = np.random.default_rng(seed)
    entries = rng.integers(0, n - hold_bars - 1, size=(samples, n_trades))
    ret = c[entries + hold_bars] / c[entries] - 1.0
    per_trade = np.clip(1.0 + sgn * leverage * size_fraction * ret - rt_fee, 0.0, None)
    return (per_trade.prod(axis=1) - 1.0) * 100.0


def matched_params(metrics, timeframe: str, initial_equity: float) -> dict:
    """전략의 **실제** 트레이드에서 귀무 조건을 뽑는다.

    귀무는 '같은 조건의 무근거 진입'이어야 한다. 횟수·보유시간·방향·레버리지·명목비율을
    임의로 정하면 비교가 무의미해진다 — 예전에 회전율 차이를 신호로 오독한 적이 있다(L).
    """
    tf_min = TIMEFRAME_MINUTES[timeframe]
    tr = list(metrics.trades)
    if not tr:
        return {"n_trades": 0, "hold_bars": 1, "side": "long", "leverage": 1,
                "size_fraction": 0.1, "tf_min": tf_min}
    holds = [max(1, round((t.exit_time - t.entry_time) / (tf_min * 60_000))) for t in tr]
    longs = sum(1 for t in tr if t.side > 0)
    lev = max(1, round(sum(t.leverage for t in tr) / len(tr)))
    # 명목/레버리지 = 증거금. 자본 대비 비율이 귀무의 노출 크기다.
    frac = sum(t.entry_price * t.qty / max(1, t.leverage) for t in tr) / len(tr) / initial_equity
    return {
        "n_trades": len(tr),
        "hold_bars": max(1, int(round(sum(holds) / len(holds)))),
        "side": "long" if longs * 2 >= len(tr) else "short",
        "leverage": lev,
        "size_fraction": min(1.0, max(0.001, frac)),
        "tf_min": tf_min,
    }


def best_of_n(null_dist: np.ndarray, n: int, rounds: int = 2000, seed: int = 1) -> np.ndarray:
    """무근거 진입 n 개 중 **최고**의 분포. 격자 탐색이 하는 일을 그대로 흉내낸 것."""
    n = max(1, int(n))
    if len(null_dist) <= 1:
        return np.asarray(null_dist, dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(null_dist), size=(rounds, n))
    return np.asarray(null_dist)[idx].max(axis=1)


def gate(strategy_return_pct: float, null_dist: np.ndarray, n_combos: int = 1,
         pct: float = 95.0, rounds: int = 2000, seed: int = 1) -> dict:
    """판정. n_combos>1 이면 '그중 최고' 문턱까지 함께 본다(다중검정 보정).

    ⚠️ **깊게 음수인 구간에서의 '귀무 초과'는 엣지가 아니다.** 전략이 −90%, 귀무가 −95%
    여도 beats 는 True 가 된다. 그래서 절대 수익률을 같이 싣는다 — 판정만 보고 채택하지
    말 것(research/README.md 의 판정 원칙 ④, 실제로 한 번 속았다).
    """
    d = np.asarray(null_dist, dtype=float)
    p = float(np.percentile(d, pct))
    out = {
        "strategyPct": round(float(strategy_return_pct), 2),
        "nullP95": round(p, 2),
        "beatsNull": bool(strategy_return_pct > p),
        "nullMedian": round(float(np.median(d)), 2),
        "percentile": round(float((d < strategy_return_pct).mean() * 100), 1),
        "combos": int(n_combos),
        # 음수 구간 경고 — 판정이 True 여도 돈을 버는 건 아니다.
        "negative": bool(strategy_return_pct <= 0),
    }
    if n_combos > 1:
        b = best_of_n(d, n_combos, rounds=rounds, seed=seed)
        bp = float(np.percentile(b, pct))
        out["bestOfNP95"] = round(bp, 2)
        out["beatsBestOfN"] = bool(strategy_return_pct > bp)
    else:
        out["bestOfNP95"] = out["nullP95"]
        out["beatsBestOfN"] = out["beatsNull"]
    return out
