"""분할 매수(가격 간격) — sizing.scaleIn. 네트워크 없음.

전체 수량을 1+N 회차로 나눠 첫 회차는 신호 때, 나머지는 첫 진입가에서 불리한 쪽으로 간격마다 지정가로 산다.
손절은 첫 진입 때 값 고정(그 너머 회차는 못 삼), 익절(%·ATR·손익비)은 평균단가에서 다시.
실거래 주문기는 아직 지원하지 않는다 → 시작 전에 분명히 거부.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import binance_math as bm               # noqa: E402
from engine import ranking, server, video_analyze  # noqa: E402
from engine.backtest import BacktestConfig, Stepper, _scale_plan, run, unsupported_reason   # noqa: E402
from engine.candles import Candles                 # noqa: E402
from engine.executor import LiveExecutor, PaperExecutor   # noqa: E402
from engine.preset import Preset                   # noqa: E402

M = 60_000
FREE = dict(initial_equity=10000.0, taker_fee=0.0, maker_fee=0.0)


def _seq(prices, wick=0.05):
    cl = np.asarray(prices, float)
    return Candles(open_time=np.arange(len(cl), dtype=np.int64) * M, open=cl.copy(), high=cl + wick, low=cl - wick,
                   close=cl, volume=np.full(len(cl), 10.0), timeframe_min=1)


def _form(side="long", **kw):
    cross = {"cross": "crossOver", "left": {"source": "close"}, "right": 99.5} if side == "long" else \
            {"cross": "crossUnder", "left": {"source": "close"}, "right": 100.5}
    p = {"symbol": "BTCUSDT", "timeframe": "1m", "days": "1", "equity": "10000", "slType": "percent", "slValue": "10",
         "tpType": "percent", "tpValue": "2", "leverage": "1", "sizeType": "equityPercent", "sizeValue": "30",
         "cooldownBars": "100", "avoidFundingMin": "0", "entryType": "taker", "levTiers": [],
         "siEnabled": True, "siCount": "2", "siType": "percent", "siValue": "1", "siMult": "1",
         "entryGroups": [{"side": side, "conditions": [cross]}]}
    p.update(kw)
    return p


def _bt(prices, side="long", **kw):
    preset = Preset.from_dict(server._build_preset(_form(side, **kw)), validate=True)
    return run(_seq(prices), preset, BacktestConfig(**FREE))


# 100 진입 → 99(1%)·98(2%) 회차 → 반등
DIP = [99] * 3 + [100, 99.5, 98.95, 98.5, 97.9, 98.5, 99, 99.5, 100, 100.5, 101, 102]


def test_ladder_average_and_retargeted_tp():
    t, = _bt(DIP).trades
    assert t.adds == 2 and t.qty == pytest.approx(30.0)            # 다 사면 분할 안 한 것과 같은 전체 수량
    assert t.entry_price == pytest.approx(99.0)                     # 100·99·98 균등 → 평균 99
    assert t.exit_reason == "take_profit" and t.exit_price == pytest.approx(99.0 * 1.02)   # 익절은 평균단가 +2%
    assert t.pnl == pytest.approx(30 * 99.0 * 0.02)


def test_without_scale_in_is_one_shot():
    t, = _bt(DIP, siEnabled=False).trades
    assert t.adds == 0 and t.entry_price == 100 and t.exit_price == pytest.approx(102)


def test_partial_fill_when_price_turns():
    t, = _bt([99] * 3 + [100, 99.5, 98.95, 99.5, 100, 101, 101.5]).trades    # 99 만 닿고 반등
    assert t.adds == 1 and t.qty == pytest.approx(20.0) and t.entry_price == pytest.approx(99.5)


def test_multiplier_weights():
    w0, rest = _scale_plan({"count": 2, "spacing": {"type": "percent", "value": 1}, "multiplier": 2}, 100.0, 1, np.nan)
    assert [w0] + [w for _, w in rest] == pytest.approx([1 / 7, 2 / 7, 4 / 7])
    assert [lvl for lvl, _ in rest] == pytest.approx([99.0, 98.0])
    _, rest = _scale_plan({"count": 2, "spacing": {"type": "atrMultiple", "value": 0.5}}, 100.0, -1, 4.0)
    assert [lvl for lvl, _ in rest] == pytest.approx([102.0, 104.0])          # 숏은 위로
    assert _scale_plan({"count": 2, "spacing": {"type": "atrMultiple", "value": 0.5}}, 100.0, 1, np.nan) is None


def test_stop_after_adds_and_levels_beyond_stop_dropped():
    """손절 2.5% · 회차 1%×3: 99·98 을 산 뒤 97.5 손절. 97 회차는 손절 너머라 못 산다(낙관 금지 — 더 산 수량으로 손절)."""
    t, = _bt([99] * 3 + [100, 99, 98, 97.4, 97], slValue="2.5", siCount="3").trades
    assert t.exit_reason == "stop_loss" and t.adds == 2
    assert t.qty == pytest.approx(30 * 3 / 4) and t.entry_price == pytest.approx(99.0)
    assert t.exit_price == pytest.approx(97.5)


def test_tp_not_in_same_bar_as_add():
    """한 1분봉이 회차(저가)와 익절가(고가)에 다 닿으면 순서를 모른다 → 익절은 다음 봉부터(보수적)."""
    preset = Preset.from_dict(server._build_preset(_form(siCount="1", tpValue="0.5")), validate=True)
    c = _seq([99] * 3 + [100, 99.5, 99.5, 99.5])
    c.low[4], c.high[4] = 98.9, 100.6        # 봉 4: 99(회차)와 평균 99.5 의 +0.5% = 99.9975 둘 다 닿음
    t, = run(c, preset, BacktestConfig(**FREE)).trades
    assert t.adds == 1 and t.exit_time > c.open_time[4] + M      # 봉 4 에서 익절하지 않았다


def test_short_mirror():
    t, = _bt([101] * 3 + [100, 100.5, 101.05, 101.5, 102.1, 101.5, 101, 100, 99, 98], side="short").trades
    assert t.adds == 2 and t.entry_price == pytest.approx(101.0) and t.exit_price == pytest.approx(101 * 0.98)


def test_liquidation_price_follows_position():
    preset = Preset.from_dict(server._build_preset(_form(leverage="20", slValue="3")), validate=True)
    ex = PaperExecutor(equity=10000.0, taker_fee=0, maker_fee=0)
    seen = []
    st = Stepper(preset, BacktestConfig(**FREE), ex, on_open=lambda p, lev: seen.append(p))
    c = _seq([99] * 3 + [100, 99.5, 98.95, 98.97])
    from engine.candles import signal_close_index
    from engine.conditions import SeriesResolver
    from engine import indicators as ind
    bar_of, is_close = signal_close_index(c, 1)
    res, atr = SeriesResolver(c), ind.atr(c.high, c.low, c.close, 14)
    for i in range(len(c)):
        st.step(c, c, bar_of, is_close, atr, res, i)
    pos = ex.position
    assert pos.fills == 2 and pos.entry_price == pytest.approx(99.5)
    assert pos.liq_price == pytest.approx(bm.liquidation_price(pos.entry_price, pos.qty, 20, 1, BacktestConfig().bracket,
                                                               wallet_balance=pos.margin))


def test_live_executor_refuses_before_start():
    preset = Preset.from_dict(server._build_preset(_form()), validate=True)
    assert not callable(getattr(LiveExecutor, "scale_in", None))
    live_like = object.__new__(LiveExecutor)                      # 키·네트워크 없이 '실거래 주문기' 자리만
    assert "분할 매수" in unsupported_reason(preset, live_like)
    with pytest.raises(ValueError, match="분할 매수"):
        Stepper(preset, BacktestConfig(), live_like)
    assert unsupported_reason(preset, PaperExecutor()) is None
    plain = Preset.from_dict(server._build_preset(_form(siEnabled=False)), validate=True)
    assert unsupported_reason(plain, live_like) is None


def test_schema_ranking_and_draft():
    p = server._build_preset(_form(siMult="2"))
    assert p["sizing"]["scaleIn"] == {"count": 2, "spacing": {"type": "percent", "value": 1.0}, "multiplier": 2.0}
    assert "분할 1+2회(1% 간격, 비중×2)" in ranking.describe(p)["sizing"]
    bad = server._build_preset(_form())
    bad["sizing"]["scaleIn"]["count"] = 11
    with pytest.raises(Exception):
        Preset.from_dict(bad, validate=True)
    draft = {"name": "ST", "timeframe": "5m",
             "groups": [{"side": "long", "conds": [{"left": {"kind": "indicator", "indicator": "RSI", "period": 14}, "op": "<",
                                                    "right": {"kind": "number", "value": 30}, "note": "RSI"}]}],
             "stop": {"type": "swing", "lookback": 20}, "take": {"type": "riskReward", "value": 2},
             "exit_long": [], "exit_short": [], "time_stop_bars": 0, "assumed": [], "excluded": [],
             "scale_in": {"count": 2, "spacing_type": "percent", "spacing_value": 1.5}}
    form, params = video_analyze._check_draft(draft)
    assert form["siEnabled"] is True and form["siValue"] == "1.5"
    assert server._build_preset(params)["sizing"]["scaleIn"]["count"] == 2
