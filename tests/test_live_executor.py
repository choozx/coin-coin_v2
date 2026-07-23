"""LiveExecutor(실거래) — 가짜 브로커로 실주문 경로 전체를 네트워크 없이 검증.

여기서 지키려는 것은 하나다: **엔진이 가정한 가격이 아니라 실제 체결로 포지션이 잡히는가.**
백테스트는 '신호봉 종가에 원하는 수량이 다 체결된다'고 낙관하지만 실거래는 안 그렇다 —
그 차이를 executor 가 흡수하지 못하면 이후 손절·청산 판정이 전부 허구 위에서 돈다.

가짜 브로커는 binance_broker.BinanceBroker 와 같은 표면만 흉내낸다(ccxt·네트워크 없음).
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.backtest import _Position                       # noqa: E402
from engine.binance_broker import Fill, OrderError, _merge   # noqa: E402
from engine.executor import LiveExecutor                    # noqa: E402


class FakeBroker:
    """주문을 받아 정해둔 체결을 돌려주는 가짜 거래소."""

    def __init__(self, fills=None, position=None, equity=1000.0,
                 min_qty=0.001, min_cost=5.0, step=0.001):
        self.fills = list(fills or [])       # 순서대로 소비. 비면 요청 수량 그대로 체결
        self.position_data = position
        self._equity = equity
        self.min_qty, self.min_cost, self.step = min_qty, min_cost, step
        self.orders = []                     # (kind, side, qty, reduce_only)
        self.leverage = None

    # -- 메타/조회 --
    def market(self):
        return {"symbol": "BTC/USDT:USDT", "quote": "USDT",
                "limits": {"amount": {"min": self.min_qty}, "cost": {"min": self.min_cost}}}

    def round_qty(self, qty):
        return round(int(qty / self.step) * self.step, 8)

    def round_price(self, p):
        return round(p, 2)

    def check_order_size(self, qty, price):
        if qty <= 0:
            raise OrderError("수량 0")
        if qty < self.min_qty:
            raise OrderError(f"최소 수량 미달 {qty}")
        if qty * price < self.min_cost:
            raise OrderError(f"최소 명목가 미달 {qty * price}")

    def equity(self, asset):
        return self._equity

    def position(self):
        return self.position_data

    def set_leverage(self, lev):
        self.leverage = lev

    def funding_paid(self, a, b):
        return 0.0

    # -- 체결 --
    def _next(self, kind, side, qty, reduce_only):
        self.orders.append((kind, side, qty, reduce_only))
        if self.fills:
            return self.fills.pop(0)
        return Fill(price=100.0, qty=qty, taker_qty=qty, fee=None, ts=1)

    def market_order(self, side, qty, reduce_only=False):
        return self._next("market", side, qty, reduce_only)

    def limit_then_market(self, side, qty, timeout_s, reduce_only=False):
        return self._next("limit", side, qty, reduce_only)


def _near(a, b, tol=1e-9):
    return abs(float(a) - float(b)) <= tol + 1e-6 * abs(float(b))


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"{exc.__name__} 가 나야 하는데 안 남")


def _pos(side=1, price=100.0, qty=1.0, lev=5):
    return _Position(side=side, entry_time=1_000, entry_price=price, qty=qty, leverage=lev,
                     margin=price * qty / lev, liq_price=80.0, stop_price=95.0, tp_price=110.0,
                     entry_fee=0.05, entry_signal_idx=3, peak=price)


def _ex(broker, **kw):
    kw.setdefault("position_path", os.path.join(tempfile.mkdtemp(prefix="livepos-"), "p.json"))
    return LiveExecutor(testnet=True, symbol="BTCUSDT", broker=broker, **kw)


def test_open_adopts_real_fill_not_engine_assumption():
    """체결가·수량·수수료가 엔진 가정과 다르면 실제 값으로 포지션이 잡혀야 한다."""
    broker = FakeBroker(fills=[Fill(price=100.4, qty=0.9, taker_qty=0.9, fee=0.45)],
                        position={"side": 1, "qty": 0.9, "entry_price": 100.4, "leverage": 5,
                                  "liq_price": 81.7, "margin": 18.07})
    ex = _ex(broker)
    p = _pos(price=100.0, qty=1.0)
    ex.open(p)
    assert ex.position is p
    assert p.entry_price == 100.4 and p.qty == 0.9      # 슬리피지·부분체결 반영
    assert p.entry_fee == 0.45                          # 거래소가 알려준 실수수료
    assert p.liq_price == 81.7                          # 청산가는 거래소 계산값을 채택
    assert broker.leverage == 5
    assert broker.orders[0][:3] == ("market", "buy", 1.0)   # 주문은 1.0, 체결은 0.9


def test_open_uses_post_only_path_when_maker():
    """maker 진입 프리셋이면 post-only 지정가(→3초 후 시장가) 경로를 타야 한다."""
    broker = FakeBroker()
    ex = _ex(broker)
    ex.open(_pos(), is_maker=True)
    assert broker.orders[0][0] == "limit"
    assert broker.orders[0][3] is False           # 진입은 reduceOnly 아님


def test_open_rejected_below_min_notional_leaves_no_position():
    """최소주문 미달이면 포지션이 생기면 안 된다(주문도 안 나가야 한다)."""
    ex = _ex(FakeBroker(min_cost=100.0))
    _raises(OrderError, lambda: ex.open(_pos(price=100.0, qty=0.5)))   # 명목 50 < 100
    assert ex.position is None
    assert ex.broker.orders == []


def test_close_records_real_exit_price_and_fee():
    broker = FakeBroker()
    ex = _ex(broker)
    ex.open(_pos(price=100.0, qty=1.0))              # 기본 체결: 100.0 x1, 수수료 미확정
    broker.fills.append(Fill(price=94.2, qty=1.0, taker_qty=1.0, fee=0.47))
    tr = ex.close(95.0, "stop_loss", 2_000)          # 엔진은 95 를 가정했지만 실제론 94.2
    assert tr.exit_price == 94.2                     # 손절 슬리피지가 그대로 기록
    assert tr.exit_reason == "stop_loss"
    assert broker.orders[-1] == ("market", "sell", 1.0, True)   # 손절은 시장가·reduceOnly
    entry_fee = 100.0 * 1.0 * ex.taker_fee           # fee=None → 공식 근사
    assert _near(tr.fees, entry_fee + 0.47)
    assert _near(tr.pnl, -5.8 - tr.fees)
    assert ex.position is None


def test_liquidation_uses_exchange_truth():
    """엔진이 '강제청산' 판정했는데 거래소엔 포지션이 없으면 주문을 내면 안 된다."""
    broker = FakeBroker()
    ex = _ex(broker)
    ex.open(_pos())
    broker.position_data = None                      # 거래소: 이미 털림
    n = len(broker.orders)
    tr = ex.close(80.0, "liquidation", 3_000)
    assert len(broker.orders) == n                   # 추가 주문 없음
    assert tr.exit_reason == "liquidation" and tr.exit_price == 80.0


def test_liquidation_still_open_falls_back_to_market_close():
    """반대로 아직 살아 있으면 시장가로 확실히 빠져나와야 한다(추정 청산가가 빗나간 경우)."""
    broker = FakeBroker(position={"side": 1, "qty": 1.0, "entry_price": 100.0, "leverage": 5,
                                  "liq_price": 79.0, "margin": 20.0})
    ex = _ex(broker)
    ex.open(_pos())
    broker.fills.append(Fill(price=79.5, qty=1.0, taker_qty=1.0, fee=0.4))
    tr = ex.close(80.0, "liquidation", 3_000)
    assert broker.orders[-1] == ("market", "sell", 1.0, True)
    assert tr.exit_price == 79.5


def test_partial_close_keeps_remainder_and_raises():
    """부분청산이면 잔량을 계속 들고 있어야 한다 — '다 닫았다'고 기록하면 유령 포지션이 남는다."""
    broker = FakeBroker()
    ex = _ex(broker)
    ex.open(_pos(qty=1.0))
    broker.fills.append(Fill(price=99.0, qty=0.4, taker_qty=0.4, fee=0.2))
    _raises(OrderError, lambda: ex.close(99.0, "signal", 2_000))
    assert ex.position is not None
    assert _near(ex.position.qty, 0.6)
    assert ex.trades == []


def test_position_sidecar_roundtrip():
    """재시작 복원용 사이드카: 진입 때 쓰고 청산 때 지운다."""
    broker = FakeBroker()
    ex = _ex(broker)
    ex.open(_pos(side=-1, price=100.0, qty=1.0))
    saved = ex.load_saved_position()
    assert saved["side"] == -1 and saved["stop"] == 95.0 and saved["symbol"] == "BTCUSDT"
    ex.close(99.0, "signal", 2_000)
    assert ex.load_saved_position() == {}


def test_merge_fills_weighted_average():
    a = Fill(price=100.0, qty=0.5, maker_qty=0.5, fee=0.01)
    b = Fill(price=102.0, qty=0.5, taker_qty=0.5, fee=0.05)
    m = _merge(a, b)
    assert _near(m.price, 101.0) and _near(m.qty, 1.0)
    assert _near(m.fee, 0.06) and not m.is_maker


def test_equity_is_cached_but_invalidated_by_trades():
    broker = FakeBroker(equity=500.0)
    ex = _ex(broker)
    assert ex.equity() == 500.0
    broker._equity = 700.0
    assert ex.equity() == 500.0                      # 캐시(3초)
    ex.open(_pos())
    assert ex.equity() == 700.0                      # 매매가 있었으면 즉시 재조회


def test_symbol_change_rebinds_and_is_blocked_while_holding():
    """심볼이 바뀌면 마진자산도 따라가야 하고, 포지션 보유 중엔 아예 막혀야 한다."""
    ex = _ex(FakeBroker())
    ex.set_symbol("ETHUSDC")
    assert ex.symbol == "ETHUSDC" and ex.quote_asset == "USDC"
    holding = _ex(FakeBroker())
    holding.open(_pos())
    _raises(RuntimeError, lambda: holding.set_symbol("ETHUSDT"))
    assert holding.symbol == "BTCUSDT"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
