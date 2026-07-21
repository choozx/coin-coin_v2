"""매매 원장의 한 거래 → 진입~청산 구간의 캔들+지표 차트 데이터.

대시보드에서 매매기록 한 줄을 펼치면 그 거래의 진입부터 청산까지를 백테스트 캔들차트처럼
(전략에 쓰인 지표까지) 보여주기 위한 데이터를 만든다. 백테스트가 쓰는 지표추출·OHLC
헬퍼(_chart_indicators / _ohlc_for_chart)를 그대로 재사용 → 차트가 백테스트와 동일하게 나온다.

지표는 진입 전 워밍업 구간까지 계산한 뒤(EMA200·SuperTrend 등 과거 필요), 표시 구간만 잘라 반환.
"""
from __future__ import annotations

from . import candle_store
from . import ledger
from .candles import TIMEFRAME_MINUTES
from .preset import load_preset_file

MINUTE_MS = 60_000


def build(trade_id: int, mode: str = "paper", ledger_path: str = None) -> dict:
    """원장의 trade_id 거래에 대한 진입~청산 구간 차트 데이터."""
    rows = ledger.load(ledger_path or ledger.LEDGER_PATH, mode=mode)
    tr = next((r for r in rows if r["id"] == int(trade_id)), None)
    if tr is None:
        raise ValueError(f"거래 없음 (id={trade_id})")

    # 전략 프리셋 로드 → 타임프레임·지표. 파일이 사라졌으면 지표 없이 캔들만.
    preset = None
    try:
        preset = load_preset_file(tr["strategy"], validate=False)
    except Exception:
        preset = None
    tf = preset.timeframe if preset else "15m"
    tf_min = TIMEFRAME_MINUTES[tf]
    symbol = tr["symbol"]

    et, xt = int(tr["entry_time"]), int(tr["exit_time"])
    dur = max(xt - et, tf_min * MINUTE_MS)
    pad = max(tf_min * 8 * MINUTE_MS, int(dur * 0.25))   # 양옆 여백(≥8봉 or 구간의 25%)
    disp0, disp1 = et - pad, xt + pad
    warm = 250 * tf_min * MINUTE_MS                      # 지표 워밍업(EMA200 등 넉넉히)

    # 캔들 확보(워밍업 포함) → 지표는 전체로 계산, 표시는 구간만.
    base = candle_store.ensure(symbol, disp0 - warm, disp1)

    from .server import _chart_indicators, _ohlc_for_chart   # 지연 임포트(순환 회피)
    ohlc, bar_min = _ohlc_for_chart(base, tf_min)
    inds = _chart_indicators(base, tf_min, preset.data) if preset else []

    # 표시 구간으로 클립(지표값은 워밍업으로 이미 정확히 계산됨)
    ohlc = [b for b in ohlc if disp0 <= b[0] <= disp1]
    for ind in inds:
        ind["data"] = [p for p in ind["data"] if disp0 <= p[0] <= disp1]

    return {
        "symbol": symbol, "timeframe": tf, "ohlcBarMin": bar_min,
        "ohlc": ohlc, "indicators": inds,
        "entry": {"time": et, "price": tr["entry_price"], "side": tr["side"]},
        "exit": {"time": xt, "price": tr["exit_price"], "reason": tr["reason"], "pnl": tr["pnl"]},
        "strategy": tr["strategy"], "leverage": tr["leverage"],
    }
