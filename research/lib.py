"""연구 하네스 — 실험 스크립트가 공유하는 최소 도구.

설계: 실험(research/exp_*.py)은 '가설 하나'만 담고, 데이터 로드·백테스트·귀무모델
같은 반복 배관은 전부 여기서 가져다 쓴다. 그래야 실험 파일이 짧게 유지되고,
백테스트가 대시보드/GUI와 '같은 엔진'을 타는 프로젝트 대전제도 지켜진다.

핵심 3함수:
  load()       — 캐시(data/candles.db)에서 1분봉 + 실제 펀딩 히스토리
  backtest()   — 프리셋 dict 하나를 실데이터로 백테스트(실수수료·실펀딩)
  null_model() — 같은 트레이드수·보유·방향의 '랜덤 진입' 몬테카를로 → 우연의 수익분포.
                 전략이 이 분포의 95%선을 못 넘으면 '엣지 아님'. (edge-research 메모의 판정도구)

주의: null_model 은 근사다(고정 보유·고정 명목비율·왕복 taker). 전략의 실제 보유시간
분포까지 맞춘 '정밀 귀무'는 refine 항목 — BACKLOG의 N0 참고.
"""
from __future__ import annotations

import numpy as np

from engine import binance_math as bm
from engine import null_model as nm
from engine import candle_store as cs
from engine.backtest import BacktestConfig, run
from engine.candles import TIMEFRAME_MINUTES, resample
from engine.preset import Preset


# ── 데이터 ────────────────────────────────────────────────────────────────
def load(symbol: str, days: float = None, start_ms: int = None, end_ms: int = None,
         with_funding: bool = True):
    """캐시에서 1분봉 로드. days 또는 (start_ms,end_ms) 중 하나.

    반환: (candles, funding_schedule)  — 네트워크 없음, 캐시 전용(없으면 수집기로 먼저 채울 것).
    """
    if days is not None:
        base = cs.load_recent(symbol, days)
    elif start_ms is not None and end_ms is not None:
        base = cs.load_range(symbol, start_ms, end_ms)
    else:
        raise ValueError("days 또는 (start_ms, end_ms) 를 줘")
    if len(base) == 0:
        raise SystemExit(f"[{symbol}] 캐시에 데이터 없음 — /collector 또는 "
                         f"`python3 -m engine.collector {symbol} --seed-days N` 로 먼저 수집")
    fsched = None
    if with_funding:
        fsched = cs.funding_schedule(symbol, int(base.open_time[0]), int(base.open_time[-1]))
    return base, fsched


# ── 백테스트 (엔진 그대로) ──────────────────────────────────────────────────
def backtest(base, preset_dict: dict, symbol: str, equity: float = 10000.0,
             funding_schedule: dict = None, maker_fee: float = None, taker_fee: float = None):
    """프리셋 dict → Metrics. 실수수료(심볼별)·실펀딩 반영. server._run_backtest 와 동일 경로.

    maker_fee/taker_fee 를 주면 심볼 기본값을 덮는다 — 'maker 로 다 체결된다면?' 같은
    상한 탐색용(taker_fee 를 maker 수준으로 낮춰 쓴다). ★ 이때 null_model 에도 같은
    수수료를 넘겨야 공정하다. 전략만 싸게 하면 귀무를 부당하게 이긴다.
    """
    preset = Preset.from_dict(preset_dict, validate=True)
    mk, tk = bm.fees_for_symbol(symbol)
    cfg = BacktestConfig(initial_equity=equity,
                         maker_fee=mk if maker_fee is None else maker_fee,
                         taker_fee=tk if taker_fee is None else taker_fee,
                         funding_schedule=funding_schedule)
    return run(base, preset, cfg)


def summarize(m) -> dict:
    """Metrics → 비교하기 좋은 dict(한 줄 로그/표에)."""
    pf = m.profit_factor
    return {
        "return%": round(m.total_return_pct, 2),
        "trades": m.num_trades,
        "win%": round(m.win_rate_pct, 1),
        "pf": None if pf == float("inf") else round(pf, 3),
        "mdd%": round(m.max_drawdown_pct, 2),
        "sharpe": round(m.sharpe(), 2),
        "fees": round(m.total_fees, 1),
        "funding": round(m.total_funding, 1),
        "liq": m.num_liquidations,
    }


def show(tag: str, m):
    """실험에서 한 줄로 결과 찍기."""
    s = summarize(m)
    print(f"{tag:<28} {s['return%']:+7.2f}%  T={s['trades']:>4}  승={s['win%']:>4}%  "
          f"PF={s['pf']}  MDD={s['mdd%']:>5}%  수수료={s['fees']:.0f}  펀딩={s['funding']:+.0f}")


# ── 귀무모델 (핵심 판정도구) ─────────────────────────────────────────────────
def null_model(base, timeframe: str, n_trades: int, hold_bars: int, side: str,
               leverage: int = 1, size_fraction: float = 0.10,
               samples: int = 2000, seed: int = 0, fee: float = None):
    """랜덤 진입 몬테카를로 → 총수익률(%) 분포.

    ★ 구현은 engine/null_model.py 에 있다. 최적화기(engine/optimize.py)도 같은 판정을
    쓰기 때문이다 — 두 곳에 각각 구현해 두면 언젠가 갈라지고, 그때 어느 쪽이 맞는지
    알 수 없게 된다(백테스트↔라이브에서 이미 겪은 일이라 판정도 한 구현으로 모은다).
    """
    return nm.simulate(base, TIMEFRAME_MINUTES[timeframe], n_trades, hold_bars, side,
                       leverage=leverage, size_fraction=size_fraction,
                       samples=samples, seed=seed, taker_fee=fee)


def verdict(strategy_return_pct: float, null_dist: np.ndarray, pct: float = 95.0) -> dict:
    """전략 수익률이 귀무분포의 pct 백분위를 넘는가 = 우연 초과 여부."""
    thresh = float(np.percentile(null_dist, pct))
    beats = strategy_return_pct > thresh
    return {
        "strategy%": round(strategy_return_pct, 2),
        f"null_p{int(pct)}%": round(thresh, 2),
        "null_median%": round(float(np.median(null_dist)), 2),
        "beats_null": beats,
        "verdict": "✅ 우연 초과" if beats else "❌ 우연 이하(엣지 아님)",
    }
