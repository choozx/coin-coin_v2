"""maker 프로브의 안전장치 — 이 도구는 **실주문을 낸다**.

전략 신호를 기다리지 않고 체결 정책만 재려고 만든 도구지만, 그 대가로 봇과 같은 계좌에
주문을 낸다. 프로브가 낸 주문은 봇의 사이드카·원장에 없어서, 겹치면 봇이 그걸 '외부 진입'
으로 인계하거나 수량을 맞추려 들며 회계가 엉킨다 — 이번 달 사고가 난 자리가 정확히 거기다.
그래서 검증할 것은 체결 로직이 아니라 **거부 조건**이다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import maker_probe                                              # noqa: E402
from engine.binance_broker import OrderError                    # noqa: E402


def _state_file(**kw):
    p = os.path.join(tempfile.mkdtemp(), "state.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(kw, f)
    return p


def test_refuses_when_trader_holds_a_position():
    """★ 포지션이 있으면 --force 로도 못 뚫는다 — 겹치면 봇 회계가 깨진다."""
    p = _state_file(position={"side": 1, "qty": 0.064}, paused=True)
    with pytest.raises(SystemExit):
        maker_probe._guard_trader(p, force=False)
    with pytest.raises(SystemExit):
        maker_probe._guard_trader(p, force=True)


def test_refuses_when_trader_is_running():
    """매매 중이면 거부. 봇이 언제 진입할지 모르는 상태에서 주문을 겹치면 안 된다."""
    p = _state_file(position=None, paused=False)
    with pytest.raises(SystemExit):
        maker_probe._guard_trader(p, force=False)


def test_force_allows_running_but_not_position():
    p = _state_file(position=None, paused=False)
    maker_probe._guard_trader(p, force=True)          # 예외 없음


def test_paused_and_flat_is_allowed():
    maker_probe._guard_trader(_state_file(position=None, paused=True), force=False)


def test_unreadable_state_does_not_crash():
    """상태를 못 읽어도 죽지는 않는다(경고하고 진행) — 도구가 상태 파일에 종속되면 안 된다."""
    maker_probe._guard_trader("/nope/does/not/exist.json", force=False)


# ---- 최소 수량: 프로브는 싸야 한다 ----

class _B:
    """수량 규칙만 흉내낸 브로커."""

    def __init__(self, min_qty=0.001, min_cost=100.0, step=0.001):
        self.min_qty, self.min_cost, self.step = min_qty, min_cost, step

    def market(self):
        return {"limits": {"amount": {"min": self.min_qty}, "cost": {"min": self.min_cost}}}

    def round_qty(self, q):
        return round(int(q / self.step) * self.step, 8)

    def check_order_size(self, qty, price):
        if qty < self.min_qty or qty * price < self.min_cost:
            raise OrderError("미달")


def test_min_qty_clears_min_notional():
    """★ 스텝 내림 때문에 최소 명목에 계속 미달하면 프로브가 영영 못 돈다 — 한 스텝씩 올린다."""
    b = _B(min_cost=100.0, step=0.001)
    q = maker_probe._min_qty(b, 76900.0)
    b.check_order_size(q, 76900.0)                    # 예외 없이 통과해야 한다
    assert q <= 0.003, f"필요 이상으로 크다: {q}"     # 최소 근처여야 한다(비용)


def test_min_qty_gives_up_loudly():
    """구할 수 없으면 조용히 0 을 돌려주지 않는다 — 0 수량 주문은 원인 모를 거부가 된다."""
    class _Impossible(_B):
        def check_order_size(self, qty, price):
            raise OrderError("항상 미달")
    with pytest.raises(SystemExit):
        maker_probe._min_qty(_Impossible(), 76900.0)
