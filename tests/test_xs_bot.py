"""tools/xs_bot.py — 가짜 거래소로 계획·리밸런스·손실 한도 확인(네트워크 없음)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import xs_bot as xb  # noqa: E402


class FakeApi:
    def __init__(self, marks, pos=None, margin=100.0, fail=()):
        self.marks = dict(marks)
        self.pos = dict(pos or {})
        self.margin = margin
        self.fail = set(fail)
        self.orders = []
        self.offset = 0

    def sync(self):
        pass

    def call(self, method, path, params=None, signed=False, retries=3):
        p = params or {}
        if path == "/fapi/v1/exchangeInfo":
            return {"symbols": [{"symbol": s, "status": "TRADING", "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.1", "minQty": "0.1", "maxQty": "1000000"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"}]} for s in self.marks]}
        if path == "/fapi/v1/premiumIndex":
            return [{"symbol": s, "markPrice": str(v)} for s, v in self.marks.items()]
        if path == "/fapi/v2/account":
            return {"positions": [{"symbol": s, "positionAmt": str(q)} for s, q in self.pos.items()],
                    "assets": [{"asset": "USDT", "walletBalance": str(self.margin), "marginBalance": str(self.margin)}],
                    "totalMarginBalance": str(self.margin)}
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": False}
        if path in ("/fapi/v1/marginType", "/fapi/v1/leverage"):
            return {}
        if path == "/fapi/v1/leverageBracket":
            return [{"brackets": [{"initialLeverage": 10}]}]
        if path == "/fapi/v1/order":
            s = p["symbol"]
            if s in self.fail:
                raise xb.ApiError(-2019, "Margin is insufficient.")
            q = float(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
            if p.get("reduceOnly") and (not self.pos.get(s) or self.pos[s] * q > 0):
                raise xb.ApiError(-2022, "ReduceOnly Order is rejected.")
            self.pos[s] = round(self.pos.get(s, 0) + q, 8)
            if not self.pos[s]:
                del self.pos[s]
            self.orders.append((s, p["side"], float(p["quantity"]), p.get("reduceOnly")))
            return {"orderId": len(self.orders), "status": "FILLED", "avgPrice": str(self.marks[s])}
        if path == "/fapi/v1/userTrades":
            return [{"qty": "1", "price": "1", "commission": "0.001", "commissionAsset": "USDT"}]
        raise KeyError(path)


@pytest.fixture(autouse=True)
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(xb, "HOME", str(tmp_path))
    monkeypatch.setattr(xb, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(xb, "ORDERS", str(tmp_path / "orders.jsonl"))
    monkeypatch.setattr(xb.time, "sleep", lambda s: None)


def _signal(monkeypatch, long, short):
    monkeypatch.setattr(xb, "this_week_signal", lambda: (1000, long, short, "장부"))


def test_first_week_enters_equal_weight_and_skips_foreign(monkeypatch):
    api = FakeApi({"A": 1.0, "B": 2.0, "C": 1.0, "D": 4.0, "BTCUSDC": 1.0}, pos={"C": 3.0}, margin=40.0)
    _signal(monkeypatch, {"A": 1, "B": 2, "BTCUSDC": 1}, {"C": 1, "D": 4})
    xb.rebalance(api, True, None, 1.0)
    # C 는 남의 포지션 · BTCUSDC 는 제외 → A·B 롱, D 숏 3개, 포지션당 40/3 = 13.33
    assert api.pos == {"A": 13.3, "B": 6.6, "C": 3.0, "D": -3.3}
    st = xb.load_state()
    assert set(st["positions"]) == {"A", "B", "D"}
    assert st["start_equity"] == 40.0 and st["week"] == 1000


def test_too_small_skips_week_without_orders(monkeypatch):
    api = FakeApi({s: 1.0 for s in "ABCDEF"}, margin=20.0)
    _signal(monkeypatch, {"A": 1, "B": 1, "C": 1}, {"D": 1, "E": 1, "F": 1})
    xb.rebalance(api, True, None, 1.0)                     # 20/6 = 3.33 < 5
    assert api.orders == [] and api.pos == {}


def test_second_week_closes_dropped_flips_and_skips_tiny_adjust(monkeypatch):
    api = FakeApi({"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0}, margin=80.0)
    _signal(monkeypatch, {"A": 1, "B": 1}, {"C": 1, "D": 1})
    xb.rebalance(api, True, None, 1.0)                     # 각 20
    assert api.pos == {"A": 20.0, "B": 20.0, "C": -20.0, "D": -20.0}
    api.margin = 84.0
    # B 빠짐 · C 숏→롱 · A 유지(84/4=21 → +1, 5% 라 건너뜀) · D 유지 · E 새 숏
    _signal(monkeypatch, {"A": 1, "C": 1}, {"D": 1, "E": 1})
    api.orders.clear()
    xb.rebalance(api, True, None, 1.0)
    stages = [(s, side, red) for s, side, _, red in api.orders]
    assert stages[0] == ("B", "SELL", "true")              # 빠진 것 먼저, reduceOnly
    assert stages[-1][0] == "E"                            # 새 진입은 마지막
    assert api.pos == {"A": 20.0, "C": 21.0, "D": -20.0, "E": -21.0}
    assert set(xb.load_state()["positions"]) == {"A", "C", "D", "E"}


def test_failed_order_continues_and_state_matches_exchange(monkeypatch):
    api = FakeApi({"A": 1.0, "B": 1.0, "C": 1.0}, margin=30.0, fail={"B"})
    _signal(monkeypatch, {"A": 1, "B": 1}, {"C": 1})
    xb.rebalance(api, True, None, 1.0)
    assert api.pos == {"A": 10.0, "C": -10.0}
    assert set(xb.load_state()["positions"]) == {"A", "C"}


def test_plan_mode_sends_nothing(monkeypatch):
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    _signal(monkeypatch, {"A": 1}, {"B": 1})
    xb.rebalance(api, False, None, 1.0)
    assert api.orders == [] and not os.path.exists(xb.STATE)


def test_capital_cap_and_gross(monkeypatch):
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=1000.0)
    _signal(monkeypatch, {"A": 1}, {"B": 1})
    xb.rebalance(api, True, 20.0, 2.0)                     # 상한 20 × 2배 ÷ 2 = 20
    assert api.pos == {"A": 20.0, "B": -20.0}


def test_loss_limit_flattens_only_own_and_halts(monkeypatch):
    api = FakeApi({"A": 1.0, "B": 1.0, "X": 1.0}, pos={"X": 5.0}, margin=30.0)
    _signal(monkeypatch, {"A": 1}, {"B": 1})
    xb.rebalance(api, True, None, 1.0)
    api.margin = 20.0                                      # −33%
    xb.check(api, 0.30)
    assert api.pos == {"X": 5.0}                           # 남의 포지션은 그대로
    st = xb.load_state()
    assert st["halted"] and st["positions"] == {}
    api.orders.clear()
    xb.rebalance(api, True, None, 1.0)                     # 정지 중엔 매매 안 함
    assert api.orders == []


def test_loss_within_limit_does_nothing(monkeypatch):
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    _signal(monkeypatch, {"A": 1}, {"B": 1})
    xb.rebalance(api, True, None, 1.0)
    api.margin = 25.0                                      # −17%
    api.orders.clear()
    xb.check(api, 0.30)
    assert api.orders == [] and not xb.load_state()["halted"]


def test_qty_format_uses_step_decimals():
    assert xb._fmt_qty(13.299999999, 0.1) == "13.3"
    assert xb._fmt_qty(62.0, 1.0) == "62"
    assert xb._fmt_qty(0.0123, 0.001) == "0.012"
