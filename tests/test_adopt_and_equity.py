"""재시작 인계 · 잔고 미상 판정 — 실사고에서 나온 회귀 테스트(네트워크 없음).

2026-09-02 사고: 배포로 트레이더가 교체되자 ① 8:46 에 잡은 포지션의 진입 시각이 재시작
시각(13:46)으로 바뀌고 ② 10배 포지션이 1배로 인계됐으며 ③ 밴 중 잔고가 0 으로 읽혀
일일 손실 가드레일이 **정확히 100.0%** 로 발동했다. 셋 다 조용한 실패였다.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import live as live_mod                              # noqa: E402
from engine import settings                                      # noqa: E402
from engine.binance_broker import _position_leverage             # noqa: E402
from engine.candles import Candles                               # noqa: E402
from engine.live import LiveTrader                               # noqa: E402


# ---- ③ 밴 중 잔고 0 → 일일 손실 100% 오발동 ----

class _Ex:
    def __init__(self, equity, trades):
        self._equity, self.trades = equity, trades

    def equity(self):
        return self._equity


class _Tr:
    """_guardrail_block 이 실제로 만지는 필드만 가진 가짜 트레이더."""
    def __init__(self, equity, trades):
        self.ex = _Ex(equity, trades)
        self._guardrail_note = None
        self._equity_unknown_warned = False


class _Trade:
    def __init__(self, pnl, exit_time):
        self.pnl, self.exit_time = pnl, exit_time


def _today_loss(pnl=-45.0):
    import time
    return [_Trade(pnl, int(time.time() * 1000))]


def _with_daily_limit(pct, fn):
    orig = settings.get_guardrails
    settings.get_guardrails = lambda *a, **k: {
        "killSwitch": False, "maxConsecutiveLosses": {"enabled": False},
        "dailyLossLimit": {"enabled": True, "pct": pct}}
    try:
        return fn()
    finally:
        settings.get_guardrails = orig


def test_zero_equity_does_not_fire_daily_loss():
    """★ 잔고 0 은 '손실 100%' 가 아니라 '모른다' 다.

    그대로 계산하면 base = -today_pnl 이 되어 **손실 크기와 무관하게 정확히 100.0%** 가
    나오고 어떤 한도든 뚫린다. 실사고에서 한도 30% 짜리가 그렇게 발동했다.
    """
    tr = _Tr(0.0, _today_loss(-45.0))
    assert _with_daily_limit(30.0, lambda: LiveTrader._guardrail_block(tr)) is None
    assert tr._equity_unknown_warned is True          # 조용히 넘기지 않는다


def test_zero_equity_100pct_was_the_old_math():
    """옛 계산이 왜 항상 100.0% 였는지 고정 — 이 성질이 사고의 지문이었다."""
    today_pnl = -45.0
    base = 0.0 - today_pnl
    assert -today_pnl / base * 100 == 100.0


def test_real_equity_still_fires():
    """잔고를 읽을 수 있으면 정상 판정한다(보류가 가드레일을 무력화하면 안 된다)."""
    tr = _Tr(100.0, _today_loss(-50.0))               # 시작 150 대비 33.3% 손실
    r = _with_daily_limit(30.0, lambda: LiveTrader._guardrail_block(tr))
    assert r and "일일 손실 33.3%" in r


def test_real_equity_under_limit_passes():
    tr = _Tr(1000.0, _today_loss(-50.0))              # 시작 1050 대비 4.8%
    assert _with_daily_limit(30.0, lambda: LiveTrader._guardrail_block(tr)) is None


# ---- ② 거래소가 레버리지를 안 줄 때 ----

def test_leverage_from_exchange_field():
    assert _position_leverage({"leverage": 10}) == 10
    assert _position_leverage({"leverage": None, "info": {"leverage": "20"}}) == 20


def test_leverage_derived_from_margin_when_missing():
    """★ 필드가 없으면 명목/증거금 으로 **계산**한다 — 추정이 아니라 거래소가 준 두 값이다."""
    assert _position_leverage({"notional": 4925.0, "initialMargin": 492.5}) == 10


def test_leverage_unknown_is_zero_not_one():
    """모르면 0. 예전엔 `or 1` 이라 10배 포지션이 1배로 인계돼 margin 이 10배가 됐다."""
    assert _position_leverage({}) == 0
    assert _position_leverage({"leverage": None, "initialMargin": 0}) == 0


# ---- ① 부분청산이 진입 시각을 지우면 안 된다 ----

class _AdoptEx:
    def __init__(self, pos, saved):
        self._pos, self._saved, self.position = pos, saved, None

    def sync_position(self):
        return self._pos

    def load_saved_position(self):
        return self._saved

    def _save_position(self):
        pass


class _AdoptTr:
    def __init__(self, pos, saved):
        self.ex = _AdoptEx(pos, saved)
        self.tf_min = 1

        class _Cfg:
            taker_fee = maker_fee = 0.0005
        self.cfg = _Cfg()


def _base(n=50):
    ot = (np.arange(n) * 60_000).astype(np.int64) + 1_000_000_000_000
    px = np.full(n, 100.0)
    return Candles(ot, px, px, px, px, np.full(n, 10.0), 1)


ENTRY_MS = 1_000_000_000_000 + 5 * 60_000


def _adopt(saved, qty=0.064, sent=None):
    pos = {"side": -1, "qty": qty, "entry_price": 76960.0, "leverage": 0,
           "liq_price": 84313.0, "margin": 0.0}
    tr = _AdoptTr(pos, saved)
    orig = live_mod.notify
    live_mod.notify = lambda m, **k: (sent.append(m) if sent is not None else None)
    try:
        LiveTrader._sync_live_position(tr, _base(), "재시작")
    finally:
        live_mod.notify = orig
    return tr.ex.position


def test_partial_close_keeps_entry_time():
    """★ 수량이 줄어도 방향이 같으면 **같은 포지션**이다 — 진입 시각을 버리면 timeStop 이 밀린다.

    실사고: 08:46 진입이 재시작 시각 13:46 으로 바뀌어 시간 손절 기준이 5시간 밀렸다.
    """
    saved = {"side": -1, "qty": 0.128, "entryTime": ENTRY_MS, "stop": 78000.0,
             "tp": 74000.0, "leverage": 10, "peak": 76500.0}
    p = _adopt(saved, qty=0.064)                       # 절반만 남음 → 수량 불일치
    assert p.entry_time == ENTRY_MS, "진입 시각은 살아야 한다"
    assert p.leverage == 10, "레버리지도 사이드카에서 살린다"
    assert np.isnan(p.stop_price), "손절가는 수량 전제가 달라졌으므로 버린다"


def test_opposite_side_discards_everything():
    """방향이 다르면 다른 포지션이다 — 그때는 전부 버리는 게 맞다."""
    saved = {"side": 1, "qty": 0.064, "entryTime": ENTRY_MS, "stop": 70000.0, "leverage": 10}
    p = _adopt(saved, qty=0.064)
    assert p.entry_time != ENTRY_MS
    assert np.isnan(p.stop_price)


def test_exact_match_restores_stop():
    """수량까지 같으면 손절/익절도 그대로 살린다(정상 경로가 안 깨졌는지)."""
    saved = {"side": -1, "qty": 0.064, "entryTime": ENTRY_MS, "stop": 78000.0,
             "tp": 74000.0, "leverage": 10, "peak": 76500.0}
    p = _adopt(saved, qty=0.064)
    assert p.entry_time == ENTRY_MS and p.stop_price == 78000.0 and p.tp_price == 74000.0
