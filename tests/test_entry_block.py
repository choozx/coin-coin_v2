"""Stepper.entry_block — '왜 진입 안 했나'의 사유가 정확한가. 네트워크 없음.

이 메서드가 진짜 판정과 로그 미리보기 **양쪽**에 쓰인다. 그래서 여기서 갈리면 로그가
판정과 다른 말을 하게 된다. 단계별 사유와 우선순위를 고정한다.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.backtest import BacktestConfig, Stepper       # noqa: E402
from engine.candles import Candles                        # noqa: E402
from engine.conditions import SeriesResolver              # noqa: E402
from engine.executor import PaperExecutor                 # noqa: E402
from engine.preset import Preset                          # noqa: E402


RSI = {"indicator": "RSI", "period": 14}
NOW = 1_700_000_000_000


def _candles(n=300):
    t = np.arange(n, dtype=float)
    close = 100.0 + t * 0.1 + np.sin(t / 3.0) * 2.0
    return Candles(open_time=(np.arange(n) * 60_000).astype(np.int64),
                   open=close, high=close + 1.0, low=close - 1.0,
                   close=close, volume=np.full(n, 10.0), timeframe_min=1)


def _stepper(entry, filt=None, gate=None):
    preset = Preset({
        "schemaVersion": "1.0", "name": "t",
        "market": {"exchange": "binance-futures", "symbol": "BTCUSDT",
                   "timeframe": "1m", "direction": "long"},
        "entry": entry, "exit": {"stopLoss": {"type": "percent", "value": 1.0}},
        "sizing": {"leverage": 3, "marginMode": "isolated",
                   "size": {"type": "equityPercent", "value": 10}},
        "filter": filt or {},
    })
    return Stepper(preset, BacktestConfig(), PaperExecutor(equity=10_000.0), entry_gate=gate)


ALWAYS = {"left": RSI, "cmp": ">", "right": -1}      # RSI 는 항상 -1 보다 크다
NEVER = {"left": RSI, "cmp": "<", "right": -1}


def test_enters_when_everything_clear():
    st = _stepper(ALWAYS)
    side, block = st.entry_block(SeriesResolver(_candles()), 250, NOW)
    assert side == 1 and block is None


def test_conditions_not_met():
    side, block = _stepper(NEVER).entry_block(SeriesResolver(_candles()), 250, NOW)
    assert side is None and block == "진입 조건 미충족"


def test_position_held_wins_over_everything():
    """포지션 보유가 최우선 사유 — 조건이 참이어도 새로 안 산다."""
    st = _stepper(ALWAYS)
    st.ex.position = object()                        # 보유 중인 척
    side, block = st.entry_block(SeriesResolver(_candles()), 250, NOW)
    assert side is None and block == "포지션 보유 중"


def test_pending_limit_order():
    st = _stepper(ALWAYS)
    st.pending = {"side": 1, "limit": 100.0, "bars_left": 3}
    _, block = st.entry_block(SeriesResolver(_candles()), 250, NOW)
    assert block == "지정가 진입 대기 중"


def test_closed_gate():
    """멈춤·가드레일은 게이트로 들어온다(라이브가 상세 사유로 덮어쓴다)."""
    _, block = _stepper(ALWAYS, gate=lambda: False).entry_block(SeriesResolver(_candles()), 250, NOW)
    assert block == "진입 게이트 닫힘"


def test_cooldown_reason_shows_progress():
    """'차단됨'이 아니라 몇 봉 남았는지가 보여야 쓸모가 있다."""
    st = _stepper(ALWAYS, filt={"cooldownBars": 10})
    st.last_exit_sb = 247                            # 3봉 전에 청산
    _, block = st.entry_block(SeriesResolver(_candles()), 250, NOW)
    assert block == "청산 쿨다운 (3/10봉 경과)"


def test_trading_hours_reason():
    st = _stepper(ALWAYS, filt={"tradingHoursUTC": [{"from": "00:00", "to": "00:01"}]})
    _, block = st.entry_block(SeriesResolver(_candles()), 250, NOW)
    assert block == "거래 시간대 아님 (filter.tradingHoursUTC)"


def test_funding_window_reason_names_the_gap():
    st = _stepper(ALWAYS, filt={"avoidFundingWindowMinutes": 600})
    _, block = st.entry_block(SeriesResolver(_candles()), 250, NOW)
    assert block.startswith("펀딩 임박") and "분" in block


def test_gate_not_consulted_while_holding():
    """게이트 호출엔 부수효과(가드레일 알림)가 있다 — 포지션 보유 중엔 부르지 않던 옛 순서 유지."""
    calls = []
    st = _stepper(ALWAYS, gate=lambda: calls.append(1) or True)
    st.ex.position = object()
    st.entry_block(SeriesResolver(_candles()), 250, NOW)
    assert calls == []


# ---- maxFundingRate 필터: 스케줄의 시각별 값을 봐야 한다 ----

from datetime import datetime, timezone                    # noqa: E402
from engine import binance_math as bm                      # noqa: E402


def _at(h, m=30):
    return int(datetime(2026, 3, 15, h, m, tzinfo=timezone.utc).timestamp() * 1000)


def _st_funding(schedule=None, const=0.0001, limit=0.0005):
    st = _stepper(ALWAYS, filt={"maxFundingRate": limit})
    st.cfg.funding_rate = const
    st.cfg.funding_schedule = schedule
    return st


def test_funding_filter_uses_the_rate_of_that_interval():
    """08시 정산이 비쌌으면 그 뒤 진입만 막힌다 — 하루 종일 막히거나 하루 종일 통과가 아니다."""
    sched = {bm.last_funding_time(_at(0)): 0.0001,      # 00시 구간: 싸다
             bm.last_funding_time(_at(9)): 0.0020}      # 08시 구간: 비싸다(한도 0.05% 초과)
    st = _st_funding(sched)
    r = SeriesResolver(_candles())
    assert st.entry_block(r, 250, _at(3))[1] is None                    # 00시 구간 → 통과
    assert "펀딩률 과다" in st.entry_block(r, 250, _at(9))[1]           # 08시 구간 → 차단
    assert st.entry_block(r, 250, _at(17))[1] is None                   # 16시 구간 → 스케줄 없음, 상수 폴백


def test_funding_filter_never_looks_ahead():
    """다음 정산의 펀딩률은 진입 시점에 확정되지 않았다 — 그걸 보면 룩어헤드고 라이브와 갈린다."""
    future_spike = {bm.last_funding_time(_at(9)): 0.0020}     # 08시 값만 있다
    st = _st_funding(future_spike)
    # 07:30 은 아직 00시 구간이다. 08시의 비싼 값을 미리 보면 안 된다.
    assert st.entry_block(SeriesResolver(_candles()), 250, _at(7))[1] is None


def test_funding_filter_falls_back_to_constant_without_schedule():
    """스케줄이 없으면(라이브 페이퍼 등) 예전처럼 상수를 본다 — 동작이 사라지진 않는다."""
    assert _st_funding(None, const=0.0020).entry_block(
        SeriesResolver(_candles()), 250, _at(9))[1].startswith("펀딩률 과다")
    assert _st_funding(None, const=0.0001).entry_block(
        SeriesResolver(_candles()), 250, _at(9))[1] is None


def test_funding_filter_reason_names_the_actual_rate():
    """사유에 상수가 아니라 그 구간의 실제 값이 찍혀야 디버깅이 된다."""
    sched = {bm.last_funding_time(_at(9)): 0.0020}
    reason = _st_funding(sched, const=0.0001).entry_block(SeriesResolver(_candles()), 250, _at(9))[1]
    assert "0.2000%" in reason and "0.0100%" not in reason
