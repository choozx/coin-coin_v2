"""이동평균 두 개 기법용 지표(SMA_SLOPE·MA_GAP·MA_CROSSES·BODY_ATR) + 방향별 청산(conditionBySide)."""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import indicators as ind                          # noqa: E402
from engine.backtest import BacktestConfig, run               # noqa: E402
from engine.candles import Candles                            # noqa: E402
from engine.preset import Preset                              # noqa: E402

M = 60_000


def test_sma_slope_sign():
    up = np.linspace(100, 200, 400)
    s = ind.sma_slope(up, 50, 10)
    assert np.isnan(s[:59]).all() and (s[60:] > 0).all()
    assert (ind.sma_slope(up[::-1], 50, 10)[60:] < 0).all()


def test_ma_gap_shrinks_when_lines_meet():
    x = np.r_[np.linspace(100, 200, 300), np.full(300, 200.0)]   # 추세 후 횡보 → 두 선이 모인다
    g = ind.ma_gap(x, 22, 200)
    assert g[250] > g[-1] and g[-1] < 0.5


def test_ma_crosses_counts_chop_not_trend():
    t = np.arange(600, dtype=float)
    trend = 100 + t
    chop = 100 + np.sin(t / 3.0) * 5
    assert np.nanmax(ind.ma_crosses(trend, 22, 200, 20)) == 0
    assert np.nanmax(ind.ma_crosses(chop, 22, 200, 20)) >= 4


def test_body_atr():
    o = np.full(50, 100.0); c = o.copy(); c[-1] = 110.0
    h = np.maximum(o, c) + 1; l = np.minimum(o, c) - 1
    b = ind.body_atr(o, h, l, c, 14)
    assert b[-1] > 3 and b[-2] == 0


def _candles(close):
    n = len(close)
    ot = np.arange(n, dtype=np.int64) * M
    c = np.asarray(close, float)
    return Candles(open_time=ot, open=c, high=c + 0.01, low=c - 0.01, close=c,
                   volume=np.ones(n), timeframe_min=1)


def _preset(side, by_side):
    when = {"left": {"source": "close"}, "cmp": ">" if side == "long" else "<", "right": 100}
    ex = {"conditionBySide": by_side}
    return Preset.from_dict({
        "schemaVersion": "1.0", "name": "t",
        "market": {"exchange": "binance-futures", "symbol": "BTCUSDT", "timeframe": "1m", "direction": side},
        "entry": when, "entryRules": [{"side": side, "when": when}], "exit": ex,
        "sizing": {"leverage": 1, "marginMode": "isolated", "size": {"type": "equityPercent", "value": 50}},
    }, validate=True)


def test_condition_by_side_uses_the_positions_side():
    by = {"long": {"left": {"source": "close"}, "cmp": "<", "right": 101},
          "short": {"left": {"source": "close"}, "cmp": ">", "right": 99}}
    cfg = BacktestConfig(initial_equity=10_000, maker_fee=0, taker_fee=0)
    # 롱: 102 에서 진입 → 100.5 로 내려오면 '롱 조건'(<101)으로 청산. 숏 조건(>99)이 쓰였다면 즉시 청산됐다.
    m = run(_candles([99, 102, 102, 102, 100.5, 100.5]), _preset("long", by), cfg)
    assert [t.exit_reason for t in m.trades][:1] == ["signal"]
    assert m.trades[0].exit_price == 100.5
