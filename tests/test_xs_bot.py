"""engine/xs_bot.py — 가짜 거래소로 계획·리밸런스·손실 한도·서비스 루프 확인(네트워크 없음)."""
import json
import os
from datetime import datetime, timezone

import pytest

from engine import xs_bot as xb
from engine import xs_mom


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
            return {"positions": [{"symbol": s, "positionAmt": str(q), "unrealizedProfit": "0", "entryPrice": "1"}
                                  for s, q in self.pos.items()],
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
def tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XS_DIR", str(tmp_path))
    monkeypatch.setattr(xb.time, "sleep", lambda s: None)
    monkeypatch.setattr(xb, "notify", lambda *a, **k: None)


CFG = dict(mode="live", gross=1.0, capital=None, max_loss=0.30, top=None)


def sig(long, short, o=1000):
    return lambda: (o, long, short)


def test_first_week_enters_equal_weight_and_skips_foreign():
    api = FakeApi({"A": 1.0, "B": 2.0, "C": 1.0, "D": 4.0, "BTCUSDC": 1.0}, pos={"C": 3.0}, margin=40.0)
    res = xb.rebalance(api, True, CFG, sig({"A": 1, "B": 2, "BTCUSDC": 1}, {"C": 1, "D": 4}))
    # C 는 남의 포지션 · BTCUSDC 는 제외 → A·B 롱, D 숏 3개, 포지션당 40/3 = 13.33
    assert api.pos == {"A": 13.3, "B": 6.6, "C": 3.0, "D": -3.3}
    st = xb.load_state()
    assert set(st["positions"]) == {"A", "B", "D"}
    assert st["start_equity"] == 40.0 and st["week"] == 1000 and res["status"] == "done"


def test_too_small_skips_week_without_orders():
    api = FakeApi({s: 1.0 for s in "ABCDEF"}, margin=20.0)
    res = xb.rebalance(api, True, CFG, sig({"A": 1, "B": 1, "C": 1}, {"D": 1, "E": 1, "F": 1}))   # 20/6 = 3.33 < 5
    assert api.orders == [] and api.pos == {} and res["status"] == "skip"
    assert xb.load_state()["week"] == 1000                 # 그 주는 쉼으로 확정


def test_second_week_closes_dropped_flips_and_skips_tiny_adjust():
    api = FakeApi({"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0}, margin=80.0)
    xb.rebalance(api, True, CFG, sig({"A": 1, "B": 1}, {"C": 1, "D": 1}))       # 각 20
    assert api.pos == {"A": 20.0, "B": 20.0, "C": -20.0, "D": -20.0}
    api.margin = 84.0
    api.orders.clear()
    # B 빠짐 · C 숏→롱 · A 유지(84/4=21 → +1, 5% 라 건너뜀) · D 유지 · E 새 숏
    xb.rebalance(api, True, CFG, sig({"A": 1, "C": 1}, {"D": 1, "E": 1}, o=2000))
    stages = [(s, side, red) for s, side, _, red in api.orders]
    assert stages[0] == ("B", "SELL", "true")              # 빠진 것 먼저, reduceOnly
    assert stages[-1][0] == "E"                            # 새 진입은 마지막
    assert api.pos == {"A": 20.0, "C": 21.0, "D": -20.0, "E": -21.0}
    assert set(xb.load_state()["positions"]) == {"A", "C", "D", "E"}


def test_failed_order_continues_and_state_matches_exchange():
    api = FakeApi({"A": 1.0, "B": 1.0, "C": 1.0}, margin=30.0, fail={"B"})
    res = xb.rebalance(api, True, CFG, sig({"A": 1, "B": 1}, {"C": 1}))
    assert api.pos == {"A": 10.0, "C": -10.0}
    assert set(xb.load_state()["positions"]) == {"A", "C"} and len(res["fails"]) == 1


def test_preview_sends_nothing_but_records_plan():
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    res = xb.rebalance(api, False, CFG, sig({"A": 1}, {"B": 1}))
    st = xb.load_state()
    assert api.orders == [] and res["status"] == "preview"
    assert [o["symbol"] for o in st["last_plan"]["orders"]] == ["A", "B"] and not st.get("positions")


def test_capital_cap_and_gross():
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=1000.0)
    xb.rebalance(api, True, dict(CFG, capital=20.0, gross=2.0), sig({"A": 1}, {"B": 1}))   # 20 × 2 ÷ 2
    assert api.pos == {"A": 20.0, "B": -20.0}


def test_loss_limit_flattens_only_own_and_halts():
    api = FakeApi({"A": 1.0, "B": 1.0, "X": 1.0}, pos={"X": 5.0}, margin=30.0)
    xb.rebalance(api, True, CFG, sig({"A": 1}, {"B": 1}))
    api.margin = 20.0                                      # −33%
    hit = xb.check(api, 0.30)
    assert hit and api.pos == {"X": 5.0}                   # 남의 포지션은 그대로
    st = xb.load_state()
    assert st["halted"] and st["positions"] == {}
    api.orders.clear()
    assert xb.rebalance(api, True, CFG, sig({"A": 1}, {"B": 1}))["status"] == "halted"
    assert api.orders == []


def test_loss_within_limit_does_nothing():
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    xb.rebalance(api, True, CFG, sig({"A": 1}, {"B": 1}))
    api.margin = 25.0                                      # −17%
    api.orders.clear()
    assert xb.check(api, 0.30) is None and api.orders == [] and not xb.load_state()["halted"]


def test_qty_format_uses_step_decimals():
    assert xb._fmt_qty(13.299999999, 0.1) == "13.3"
    assert xb._fmt_qty(62.0, 1.0) == "62"
    assert xb._fmt_qty(0.0123, 0.001) == "0.012"


def _ms(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


def test_due_only_after_tuesday_0015_once_per_week():
    mon = _ms(2026, 11, 16)                                # 월요일 봉 시작
    assert xs_mom.last_monday_bar(_ms(2026, 11, 17, 0, 20)) == mon
    assert not xb.due({}, _ms(2026, 11, 17, 0, 10))        # 화 00:10 — 아직
    assert xb.due({}, _ms(2026, 11, 17, 0, 16))
    assert not xb.due({"week": mon}, _ms(2026, 11, 19))    # 이번 주 했음
    assert xb.due({"week": mon}, _ms(2026, 11, 24, 0, 16))  # 다음 주 화


def _run_loop(monkeypatch, api, mode, times):
    calls = []
    monkeypatch.setattr(xb, "this_week_signal", lambda: (xs_mom.last_monday_bar(times[0]), {"A": 1.0}, {"B": 1.0}))
    real = xb.rebalance

    def spy(api_, go, cfg, signal_fn=None):
        calls.append(go)
        return real(api_, go, cfg, xb.this_week_signal)
    monkeypatch.setattr(xb, "rebalance", spy)
    it = iter(times)
    xb.loop(dict(CFG, mode=mode), api=api, sleep=lambda s: None, now=lambda: next(it), max_iter=len(times))
    return calls


def test_loop_live_rebalances_once_and_writes_status(monkeypatch):
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    t = _ms(2026, 11, 17, 0, 16)
    calls = _run_loop(monkeypatch, api, "live", [t, t + 60_000, t + 2 * 3_600_000])
    assert calls == [True]                                 # 같은 주엔 한 번만
    assert api.pos == {"A": 15.0, "B": -15.0}
    status = json.load(open(os.path.join(os.environ["XS_DIR"], "status.json")))
    assert status["mode"] == "live" and len(status["positions"]) == 2 and status["next_rebalance"].startswith("2026-11-24")


def test_loop_plan_mode_never_orders(monkeypatch):
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    t = _ms(2026, 11, 17, 0, 16)
    calls = _run_loop(monkeypatch, api, "plan", [t, t + 2 * 3_600_000])
    assert calls == [False] and api.orders == []
    assert xb.load_state()["last_plan"]["orders"]


def test_loop_before_tuesday_does_nothing(monkeypatch):
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    calls = _run_loop(monkeypatch, api, "live", [_ms(2026, 11, 17, 0, 5)])
    assert calls == [] and api.orders == []
