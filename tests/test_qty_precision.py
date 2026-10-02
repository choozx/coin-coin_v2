"""round_qty — float 합산 잡음이 스텝 하나를 깎아 먹지 않아야 한다.

실측(테스트넷 2026-09-21, BTCUSDC 스텝 0.001): maker 0.04 + taker 0.018 = 0.057999999999999996.
ccxt 의 amount_to_precision 은 repr 문자열을 절사하므로 0.057 이 됐고, 청산이 0.001 을 남겼다.
그 잔량을 동기화가 새 포지션으로 인계해 원장에 같은 진입가의 거래가 두 건 생겼다.
"""
from __future__ import annotations

import os
import sys
from decimal import ROUND_DOWN, Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.binance_broker import BinanceBroker                # noqa: E402


class _TruncatingCCXT:
    """ccxt TRUNCATE + TICK_SIZE 와 같은 동작 — repr 문자열을 스텝 단위로 내림."""

    def __init__(self, step="0.001"):
        self.step = Decimal(step)

    def amount_to_precision(self, symbol, qty):
        return str(Decimal(repr(qty)).quantize(self.step, rounding=ROUND_DOWN))


class _Broker(BinanceBroker):
    def __init__(self):
        super().__init__("k", "s", testnet=True, symbol="BTCUSDC")
        self._fake = _TruncatingCCXT()

    def client(self):
        return self._fake

    def market(self):
        return {"symbol": "BTC/USDC:USDC"}


def test_fake_reproduces_ccxt_truncation():
    # 고치기 전 동작의 전제: 잡음 낀 float 를 그대로 넘기면 한 스텝이 깎인다.
    assert _TruncatingCCXT().amount_to_precision("x", 0.04 + 0.018) == "0.057"


def test_float_sum_noise_does_not_lose_a_step():
    b = _Broker()
    assert b.round_qty(0.04 + 0.018) == 0.058          # 청산: 보유 0.058 을 전량
    assert b.round_qty(0.059 - 0.04) == 0.019          # 진입: 남은 0.019 를 전량


def test_still_truncates_real_fractions():
    b = _Broker()
    assert b.round_qty(0.05900962) == 0.059            # 사이징 수량은 여전히 내림
    assert b.round_qty(0.0579999) == 0.057             # 잡음이 아닌 진짜 미달분은 깎는다
