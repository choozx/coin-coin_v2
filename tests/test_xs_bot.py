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
        self.income = []

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
        if path == "/fapi/v1/ticker/bookTicker":
            m = self.marks[p["symbol"]]
            return {"bidPrice": str(m * 0.999), "askPrice": str(m * 1.001)}
        if path == "/fapi/v1/income":
            return [r for r in self.income if p["startTime"] <= r["time"] <= p["endTime"]]
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


# ── 실매매 ↔ 규칙 대조 ────────────────────────────────────────────────────
W = _ms(2026, 11, 16)
DAY = 86_400_000


def _jl(name, rows):
    with open(os.path.join(os.environ["XS_DIR"], name), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _fill(s, side, qty, px, mid, t, week=W, target=None, fee=0.005):
    return dict(t=t, symbol=s, side=side, qty=qty, avgPrice=px, mid=mid, fee=fee, feeAsset="USDT",
                week=week, target=target, stage="진입")


def test_compare_decomposes_paper_vs_live(monkeypatch):
    closes = {("A", W): 1.0, ("B", W): 2.0, ("A", W + 7 * DAY): 1.10, ("B", W + 7 * DAY): 1.90}
    monkeypatch.setattr(xb, "_day_close", lambda s, t: closes.get((s, t)))
    t = W + 2 * DAY                                        # 하루 늦게 체결
    _jl("weeks.jsonl", [dict(week=W, capital=20.0, gross=1, targets={"A": 10.0, "B": -5.0}, expect={"A": 1.0, "B": 2.0})])
    _jl("orders.jsonl", [_fill("A", "BUY", 10, 1.02, 1.01, t, target=10.0), _fill("B", "SELL", 5, 1.98, 1.99, t, target=-5.0)])
    api = FakeApi({"A": 1.1, "B": 1.9})
    api.income = [dict(symbol="A", income="-0.02", time=t + 1, tranId=1), dict(symbol="Z", income="0.5", time=t + 1, tranId=2)]
    (r,) = xb.compare_all(api, now_ms=W + 9 * DAY)
    assert r["done"]
    assert r["paper"] == pytest.approx(1.5)                # 10×0.10 + (−5)×(−0.10)
    assert r["delay"] == pytest.approx(-0.15) and r["slip"] == pytest.approx(-0.15)
    assert r["fee"] == pytest.approx(-0.01) and r["qty"] == pytest.approx(0)
    assert r["live"] == pytest.approx(10 * (1.10 - 1.02) - 5 * (1.90 - 1.98) - 0.01)   # 실제 체결가로 직접 계산과 같다
    assert r["fund"] == pytest.approx(-0.02)               # 남의 심볼 Z 는 빼고
    assert "XS 대조" in xb.fmt_compare(r)


def test_compare_rebuilds_week_from_orders_and_carries_positions(monkeypatch):
    W2 = W + 7 * DAY
    closes = {("A", W): 1.0, ("B", W): 1.0, ("A", W2): 1.2, ("B", W2): 0.8, ("A", W2 + 7 * DAY): 1.3, ("B", W2 + 7 * DAY): 0.9,
              ("C", W2): 2.0}
    monkeypatch.setattr(xb, "_day_close", lambda s, t: closes.get((s, t)))
    t1, t2 = W + DAY + 900_000, W2 + DAY + 900_000
    # 1주차: weeks.jsonl 없음 → 주문으로 복원. 2주차: A 유지(주문 없음) · B 청산 · C 새 롱.
    _jl("orders.jsonl", [_fill("A", "BUY", 10, 1.0, 1.0, t1, target=10.0), _fill("B", "SELL", 10, 1.0, 1.0, t1, target=-10.0),
                         dict(_fill("B", "BUY", 10, 0.8, 0.8, t2, week=W2, target=0.0), stage="청산"),
                         _fill("C", "BUY", 5, 2.0, 2.0, t2, week=W2, target=5.0)])
    _jl("weeks.jsonl", [dict(week=W2, capital=20.0, targets={"A": 10.0, "C": 5.0}, expect={"A": 1.2, "C": 2.0})])
    closes[("C", W2 + 7 * DAY)] = 2.2
    r1, r2 = xb.compare_all(FakeApi({}), now_ms=W2 + 9 * DAY, funding=False)
    assert r1["rebuilt"] and r1["paper"] == pytest.approx(10 * 0.2 + 10 * 0.2)
    assert r1["live"] == pytest.approx(r1["paper"] - 0.01)  # 종가 = 체결가 → 수수료만
    # 2주차: A 는 1주차에서 넘어온 수량(주문 없음)으로 계산 · B 는 청산돼 끝 수량 0 = 목표 0
    assert r2["paper"] == pytest.approx(10 * 0.1 + 5 * 0.2) and r2["qty"] == pytest.approx(0)
    assert r2["live"] == pytest.approx(r2["paper"] - 0.01)


def test_rebalance_logs_week_targets_and_book_mid():
    api = FakeApi({"A": 1.0, "B": 2.0}, margin=40.0)
    xb.rebalance(api, True, CFG, sig({"A": 1.0}, {"B": 2.0}))
    (wk,) = xb._read_jsonl("weeks.jsonl")
    assert wk["week"] == 1000 and wk["targets"] == {"A": 20.0, "B": -10.0} and wk["expect"] == {"A": 1.0, "B": 2.0}
    assert all(o["mid"] for o in xb._read_jsonl("orders.jsonl"))
