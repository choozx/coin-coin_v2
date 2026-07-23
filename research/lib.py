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
             funding_schedule: dict = None):
    """프리셋 dict → Metrics. 실수수료(심볼별)·실펀딩 반영. server._run_backtest 와 동일 경로."""
    preset = Preset.from_dict(preset_dict, validate=True)
    maker_fee, taker_fee = bm.fees_for_symbol(symbol)
    cfg = BacktestConfig(initial_equity=equity, maker_fee=maker_fee, taker_fee=taker_fee,
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
               samples: int = 2000, seed: int = 0):
    """랜덤 진입 몬테카를로 → 총수익률(%) 분포.

    전략과 '같은 조건'(트레이드수·보유봉수·방향·레버리지·명목비율)의 아무 근거 없는
    진입이 우연히 내는 수익 분포. 전략의 실제 수익률이 이 분포 대비 어디인지가 판정.

    모델(근사): 각 트레이드 = 상위TF 봉 무작위 진입 → hold_bars 뒤 청산.
      per-trade 수익(자본 대비) = side*lev*frac*(exit/entry-1) - 왕복 taker 수수료.
      equity 복리. side: 'long'|'short'.  frac: equityPercent/100.
    """
    tf_min = TIMEFRAME_MINUTES[timeframe]
    c = resample(base, tf_min).close.astype(np.float64)
    n = len(c)
    if n <= hold_bars + 1 or n_trades <= 0:
        return np.zeros(1)
    _, taker = _taker_placeholder()
    rt_fee = 2 * taker * leverage * size_fraction        # 왕복 taker (진입+청산)
    sgn = 1.0 if side == "long" else -1.0
    rng = np.random.default_rng(seed)
    hi = n - hold_bars - 1
    entries = rng.integers(0, hi, size=(samples, n_trades))
    entry_px = c[entries]
    exit_px = c[entries + hold_bars]
    ret = exit_px / entry_px - 1.0                        # 봉 가격수익률
    per_trade = 1.0 + sgn * leverage * size_fraction * ret - rt_fee
    per_trade = np.clip(per_trade, 0.0, None)             # 청산(자본소진) 바닥
    final = per_trade.prod(axis=1)                        # 복리
    return (final - 1.0) * 100.0


def _taker_placeholder():
    # null_model 은 심볼을 모르므로 기본 taker 를 쓴다. 심볼별로 정밀히 하려면
    # null_model 에 taker 인자를 넘기도록 확장(대부분 결론은 안 바뀜 — 수수료 지배가 크므로).
    return bm.DEFAULT_MAKER_FEE, bm.DEFAULT_TAKER_FEE


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
