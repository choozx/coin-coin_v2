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
            return [r for r in self.income if p["startTime"] <= r["time"] <= p["endTime"]
                    and r.get("incomeType", "FUNDING_FEE") == p.get("incomeType", r.get("incomeType", "FUNDING_FEE"))]
        if path == "/fapi/v1/userTrades":
            return [{"qty": "1", "price": "1", "commission": "0.001", "commissionAsset": "USDT"}]
        raise KeyError(path)


@pytest.fixture(autouse=True)
def tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XS_DIR", str(tmp_path))
    monkeypatch.setattr(xb.time, "sleep", lambda s: None)
    monkeypatch.setattr(xb, "notify", lambda *a, **k: None)

    def no_net(*a, **k):
        raise AssertionError("테스트가 실제 바이낸스 API 를 부름")
    monkeypatch.setattr(xs_mom, "_get", no_net)


CFG = dict(mode="live", gross=1.0, capital=None, max_loss=0.30, top=None, split=False)
DAY = 86_400_000


def _ms(*a):
    return int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)


MON = _ms(2026, 11, 16)                                    # 월요일 봉 시작
NOW = _ms(2026, 11, 17, 0, 16)                             # 화 00:16 UTC — 리밸런스 차례


def sig(long, short):
    return lambda o: (long, short)


def rb(api, go, cfg, signal_fn, now=NOW):
    return xb.rebalance(api, go, cfg, signal_fn, now_ms=now)


def test_first_week_enters_equal_weight_and_skips_foreign():
    api = FakeApi({"A": 1.0, "B": 2.0, "C": 1.0, "D": 4.0, "BTCUSDC": 1.0}, pos={"C": 3.0}, margin=40.0)
    res = rb(api, True, CFG, sig({"A": 1, "B": 2, "BTCUSDC": 1}, {"C": 1, "D": 4}))
    # C 는 남의 포지션 · BTCUSDC 는 제외 → A·B 롱, D 숏 3개, 포지션당 40/3 = 13.33
    assert api.pos == {"A": 13.3, "B": 6.6, "C": 3.0, "D": -3.3}
    st = xb.load_state()
    assert set(st["positions"]) == {"A", "B", "D"}
    assert st["start_equity"] == 40.0 and st["week"] == MON and res["status"] == "done"


def test_too_small_skips_week_without_orders():
    api = FakeApi({s: 1.0 for s in "ABCDEF"}, margin=20.0)
    res = rb(api, True, CFG, sig({"A": 1, "B": 1, "C": 1}, {"D": 1, "E": 1, "F": 1}))   # 20/6 = 3.33 < 5
    assert api.orders == [] and api.pos == {} and res["status"] == "skip"
    assert xb.load_state()["week"] == MON                  # 그 주는 쉼으로 확정


def test_second_week_closes_dropped_flips_and_skips_tiny_adjust():
    api = FakeApi({"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0}, margin=80.0)
    rb(api, True, CFG, sig({"A": 1, "B": 1}, {"C": 1, "D": 1}))       # 각 20
    assert api.pos == {"A": 20.0, "B": 20.0, "C": -20.0, "D": -20.0}
    api.margin = 84.0
    api.orders.clear()
    # B 빠짐 · C 숏→롱 · A 유지(84/4=21 → +1, 5% 라 건너뜀) · D 유지 · E 새 숏
    rb(api, True, CFG, sig({"A": 1, "C": 1}, {"D": 1, "E": 1}), now=NOW + 7 * DAY)
    stages = [(s, side, red) for s, side, _, red in api.orders]
    assert stages[0] == ("B", "SELL", "true")              # 빠진 것 먼저, reduceOnly
    assert stages[-1][0] == "E"                            # 새 진입은 마지막
    assert api.pos == {"A": 20.0, "C": 21.0, "D": -20.0, "E": -21.0}
    assert set(xb.load_state()["positions"]) == {"A", "C", "D", "E"}


def test_failed_order_continues_and_state_matches_exchange():
    api = FakeApi({"A": 1.0, "B": 1.0, "C": 1.0}, margin=30.0, fail={"B"})
    res = rb(api, True, CFG, sig({"A": 1, "B": 1}, {"C": 1}))
    assert api.pos == {"A": 10.0, "C": -10.0}
    assert set(xb.load_state()["positions"]) == {"A", "C"} and len(res["fails"]) == 1


def test_preview_sends_nothing_but_records_plan():
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    res = rb(api, False, CFG, sig({"A": 1}, {"B": 1}))
    st = xb.load_state()
    assert api.orders == [] and res["status"] == "preview"
    assert [o["symbol"] for o in st["last_plan"]["orders"]] == ["A", "B"] and not st.get("positions")


def test_capital_cap_and_gross():
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=1000.0)
    rb(api, True, dict(CFG, capital=20.0, gross=2.0), sig({"A": 1}, {"B": 1}))   # 20 × 2 ÷ 2
    assert api.pos == {"A": 20.0, "B": -20.0}


def test_loss_limit_flattens_only_own_and_halts():
    api = FakeApi({"A": 1.0, "B": 1.0, "X": 1.0}, pos={"X": 5.0}, margin=30.0)
    rb(api, True, CFG, sig({"A": 1}, {"B": 1}))
    api.margin = 20.0                                      # −33%
    hit = xb.check(api, 0.30)
    assert hit and api.pos == {"X": 5.0}                   # 남의 포지션은 그대로
    st = xb.load_state()
    assert st["halted"] and st["positions"] == {}
    api.orders.clear()
    assert rb(api, True, CFG, sig({"A": 1}, {"B": 1}))["status"] == "halted"
    assert api.orders == []


def test_loss_within_limit_does_nothing():
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    rb(api, True, CFG, sig({"A": 1}, {"B": 1}))
    api.margin = 25.0                                      # −17%
    api.orders.clear()
    assert xb.check(api, 0.30) is None and api.orders == [] and not xb.load_state()["halted"]


def test_qty_format_uses_step_decimals():
    assert xb._fmt_qty(13.299999999, 0.1) == "13.3"
    assert xb._fmt_qty(62.0, 1.0) == "62"
    assert xb._fmt_qty(0.0123, 0.001) == "0.012"


def test_due_only_after_tuesday_0015_once_per_week():
    mon = MON
    assert xs_mom.last_monday_bar(_ms(2026, 11, 17, 0, 20)) == mon
    assert not xb.due({}, _ms(2026, 11, 17, 0, 10))        # 화 00:10 — 아직
    assert xb.due({}, _ms(2026, 11, 17, 0, 16))
    assert not xb.due({"week": mon}, _ms(2026, 11, 19))    # 이번 주 했음
    assert xb.due({"week": mon}, _ms(2026, 11, 24, 0, 16))  # 다음 주 화


def _run_loop(monkeypatch, api, mode, times):
    calls = []
    monkeypatch.setattr(xb, "bar_signal", lambda o: ({"A": 1.0}, {"B": 1.0}))
    real = xb.rebalance

    def spy(api_, go, cfg, signal_fn=None, now_ms=None):
        calls.append(go)
        return real(api_, go, cfg, signal_fn, now_ms=now_ms)
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
    rb(api, True, CFG, sig({"A": 1.0}, {"B": 2.0}))
    (wk,) = xb._read_jsonl("weeks.jsonl")
    assert wk["week"] == MON and wk["hold"] == 7 and wk["targets"] == {"A": 20.0, "B": -10.0} and wk["expect"] == {"A": 1.0, "B": 2.0}
    assert all(o["mid"] for o in xb._read_jsonl("orders.jsonl"))


# ── 몫(요일 분산 XSD) ─────────────────────────────────────────────────────
def test_old_state_migrates_to_seven_monday_slices_without_orders():
    """옛 state.json(몫 없음) — 7몫이 같은 월요일 목록을 1/7 씩. 같은 목록이면 다음 주 주문이 생기지 않는다."""
    os.makedirs(os.environ["XS_DIR"], exist_ok=True)
    with open(os.path.join(os.environ["XS_DIR"], "state.json"), "w") as f:
        json.dump(dict(start_equity=40.0, halted=False, week=MON, positions={"A": 20.0, "B": -20.0}, plan_week=MON), f)
    st = xb.load_state()
    assert [sl["day"] for sl in st["slices"]] == [0] * 7 and all(sl["bar"] == MON for sl in st["slices"])
    assert sum(sl["qty"]["A"] for sl in st["slices"]) == pytest.approx(20.0) and st["plan_bars"] == [MON] * 7
    assert not xb.due(st, NOW) and xb.due(st, NOW + 7 * DAY)
    api = FakeApi({"A": 1.0, "B": 1.0}, pos={"A": 20.0, "B": -20.0}, margin=40.0)
    res = rb(api, True, CFG, sig({"A": 1}, {"B": 1}), now=NOW + 7 * DAY)
    assert res["status"] == "done" and api.orders == [] and api.pos == {"A": 20.0, "B": -20.0}


def test_monday_mode_170_at_2x_sizes_per_coin_not_per_slice():
    """$170 · 2배 · 30개 → 코인당 $11.33(몫 하나분 $1.6 으로 판정하면 매주 쉬게 된다)."""
    longs = {f"L{i}": 1.0 for i in range(15)}
    shorts = {f"S{i}": 1.0 for i in range(15)}
    api = FakeApi({**longs, **shorts}, margin=170.0)
    res = rb(api, True, dict(CFG, gross=2.0), sig(longs, shorts))
    assert res["status"] == "done" and res["per"] == pytest.approx(170 * 2 / 30)
    assert api.pos["L0"] == 11.3 and api.pos["S0"] == -11.3 and len(api.pos) == 30


def _wd_sig(o):
    """요일마다 다른 목록 + 늘 롱인 A."""
    k = xs_mom.weekday(o)
    return {"A": 1.0, f"L{k}": 1.0}, {f"S{k}": 1.0}


def _wd_marks():
    return {"A": 1.0, **{f"L{k}": 1.0 for k in range(7)}, **{f"S{k}": 1.0 for k in range(7)}}


def _step(api, cfg, t):
    """서비스 루프 한 바퀴(분산 전환 → 차례면 교체)."""
    st = xb.load_state()
    msg = xb.apply_split(st, cfg, t)
    if msg:
        xb.save_state(st)
    if xb.due(st, t):
        return rb(api, True, cfg, _wd_sig, now=t)
    return None


def test_switch_to_split_no_orders_then_one_slice_per_day():
    api = FakeApi(_wd_marks(), margin=700.0)
    rb(api, True, CFG, _wd_sig)                            # 화 11-17: 월요일 하나 — 7몫 모두 월요일 목록
    full = 700 / 3
    assert api.pos == {"A": 233.3, "L0": 233.3, "S0": -233.3}
    on = dict(CFG, split=True)
    api.orders.clear()
    assert _step(api, on, _ms(2026, 11, 18, 12)) is None   # 수 12:00 켬 — 주문 없음 · 오늘 차례인 몫 없음
    st = xb.load_state()
    assert api.orders == [] and st["split_since"] and [sl["day"] for sl in st["slices"]] == list(range(7))
    # 목 00:16(수요일 봉) → 금(목) → 토(금) → 일(토) → 월(일) → 화(월) → 수(화): 하루 한 몫씩
    order = [2, 3, 4, 5, 6, 0, 1]
    for i, k in enumerate(order):
        api.orders.clear()
        t = _ms(2026, 11, 19, 0, 16) + i * DAY
        res = _step(api, on, t)
        assert res["status"] == "done" and res["picks"] == [(k, xs_mom.last_bar(t, k))]
        assert not xb.due(xb.load_state(), t + 3_600_000)  # 같은 날 두 번 안 한다
        touched = {s for s, *_ in api.orders}
        if k == 0:                                         # 월요일 몫은 같은 목록(L0·S0·A) → 크기 그대로
            assert touched == set()
        else:
            assert touched == {"L0", "S0", f"L{k}", f"S{k}"}   # 월요일 목록 1/7 줄이고 그 요일 목록 1/7 진입 · A 그대로
    assert api.pos["A"] == pytest.approx(233.3)
    for k in range(7):
        assert api.pos[f"L{k}"] == pytest.approx(full / 7, abs=0.1) and api.pos[f"S{k}"] == pytest.approx(-full / 7, abs=0.1)
    assert xb._read_jsonl("weeks.jsonl")[-1]["hold"] == 1


def test_switch_waits_until_due_monday_rebalance_is_done():
    """화 00:16 — 월요일 리밸런스 차례면 그걸 먼저(7몫 모두 새 월요일 목록) 하고 그다음에 분산으로."""
    api = FakeApi(_wd_marks(), margin=700.0)
    rb(api, True, CFG, _wd_sig)
    on = dict(CFG, split=True)
    t = NOW + 7 * DAY                                      # 다음 주 화 00:16
    st = xb.load_state()
    assert xb.apply_split(st, on, t) is None and not st.get("split_since")
    _step(api, on, t)                                      # 이 바퀴는 월요일 하나로 교체
    st = xb.load_state()
    assert not st.get("split_since") and all(sl["bar"] == MON + 7 * DAY for sl in st["slices"])
    api.orders.clear()
    assert _step(api, on, t + 60_000) is None and xb.load_state()["split_since"] and api.orders == []


def test_split_request_file_is_one_way(monkeypatch):
    """--split-on --yes 는 요청 파일만 남긴다 → 서비스가 바꾼 뒤엔 파일이 없어져도 7몫으로 계속."""
    api = FakeApi(_wd_marks(), margin=700.0)
    rb(api, True, CFG, _wd_sig)
    monkeypatch.setattr(xb.sys, "argv", ["xs_bot", "--split-on", "--yes"])
    monkeypatch.setattr(xb.Api, "from_env", classmethod(lambda cls: api))
    monkeypatch.setattr("engine.env.load_dotenv", lambda *a, **k: 0)
    xb.main()
    assert xb.split_requested() and not xb.load_state().get("split_since")    # 파일만 — state.json 은 서비스가 쓴다
    assert xb.config()["split"]
    _step(api, xb.config(), _ms(2026, 11, 18, 12))
    os.remove(os.path.join(os.environ["XS_DIR"], "split_on"))
    res = _step(api, CFG, _ms(2026, 11, 19, 0, 16))         # 요청이 없어도 수요일 몫만 교체(월요일 하나로 되돌아가지 않음)
    st = xb.load_state()
    assert [sl["day"] for sl in st["slices"]] == list(range(7)) and res["picks"] == [(2, _ms(2026, 11, 18))]
    snap = xb.snapshot(api, dict(CFG, mode="live"))
    assert snap["split"] and snap["schedule"].startswith("요일 분산") and len(snap["slices"]) == 7


def test_loop_picks_up_split_request(monkeypatch):
    api = FakeApi(_wd_marks(), margin=700.0)
    rb(api, True, CFG, _wd_sig)
    os.makedirs(os.environ["XS_DIR"], exist_ok=True)
    open(os.path.join(os.environ["XS_DIR"], "split_on"), "w").close()
    monkeypatch.setattr(xb, "bar_signal", _wd_sig)
    api.orders.clear()
    ts = iter([_ms(2026, 11, 18, 12), _ms(2026, 11, 19, 0, 16)])
    xb.loop(dict(CFG), api=api, sleep=lambda s: None, now=lambda: next(ts), max_iter=2)
    assert xb.load_state()["split_since"] and {s for s, *_ in api.orders} == {"L0", "S0", "L2", "S2"}


def test_split_slice_too_small_rests_and_keeps_old_holdings():
    api = FakeApi(_wd_marks(), margin=70.0)                # 70/3 = 23.3 — 월요일 하나는 돌고, 몫 하나분 3.3 은 $5 미만
    rb(api, True, CFG, _wd_sig)
    on = dict(CFG, split=True)
    _step(api, on, _ms(2026, 11, 18, 12))
    api.orders.clear()
    t = _ms(2026, 11, 19, 0, 16)
    res = _step(api, on, t)
    assert res["status"] == "skip" and "이 몫" in res["warn"] and api.orders == []
    st = xb.load_state()
    assert st["slices"][2]["bar"] == _ms(2026, 11, 18) and set(st["slices"][2]["qty"]) == {"A", "L0", "S0"}
    assert not xb.due(st, t + 3_600_000)


def test_crash_mid_orders_reruns_only_remaining():
    """주문 중간에 죽으면 몫을 확정하지 않는다 → 다시 돌면 남은 차이만 주문(이미 낸 건 다시 안 냄)."""
    api = FakeApi({"A": 1.0, "B": 1.0, "C": 1.0}, margin=30.0)

    class Boom(Exception):
        pass
    real = api.call

    def call(method, path, params=None, signed=False, retries=3):
        if path == "/fapi/v1/order" and params["symbol"] == "C" and not api.boomed:
            api.boomed = True
            raise Boom("네트워크 끊김")
        return real(method, path, params, signed, retries)
    api.boomed = False
    api.call = call
    with pytest.raises(Boom):
        rb(api, True, CFG, sig({"A": 1, "B": 1}, {"C": 1}))
    st = xb.load_state()
    assert api.pos == {"A": 10.0, "B": 10.0} and xb.due(st, NOW) and st["slices"][0]["qty"] == {}
    api.orders.clear()
    rb(api, True, CFG, sig({"A": 1, "B": 1}, {"C": 1}))
    assert [s for s, *_ in api.orders] == ["C"] and api.pos == {"A": 10.0, "B": 10.0, "C": -10.0}
    assert not xb.due(xb.load_state(), NOW)


def test_first_live_in_split_mode_enters_monday_list_then_splits():
    """처음 켤 때(몫 기록 없음)도 같은 규칙 — 차례인 월요일 하나로 먼저 전부 진입(묵은 요일 신호로 들어가지 않음), 그 뒤 분산."""
    api = FakeApi(_wd_marks(), margin=700.0)
    on = dict(CFG, split=True)
    t = _ms(2026, 11, 19, 12)
    st = xb.load_state()
    assert xb.apply_split(st, on, t) is None               # 기록이 없어 7몫 모두 차례 → 먼저 진입
    res = rb(api, True, on, _wd_sig, now=t)
    assert sorted(k for k, _ in res["picks"]) == list(range(7))
    assert api.pos == {"A": 233.3, "L0": 233.3, "S0": -233.3}
    st = xb.load_state()
    assert xb.apply_split(st, on, t + 60_000)
    assert [sl["bar"] for sl in st["slices"]] == [MON] + [xs_mom.last_bar(t, k) for k in range(1, 7)]
    assert not xb.due(st, t + 60_000)


def test_flatten_clears_slices():
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    rb(api, True, CFG, sig({"A": 1}, {"B": 1}))
    st = xb.load_state()
    xb.flatten(api, xb.Market(api), st, "수동")
    assert all(sl["qty"] == {} for sl in xb.load_state()["slices"]) and api.pos == {}


def test_weekly_merges_daily_periods():
    base = dict(capital=100.0, notional=0.0, done=True, rebuilt=False, miss=[], skipped={}, worst=[], other_fee={},
                paper=1.0, delay=0.1, slip=-0.1, qty=0.0, fee=-0.05, fund=0.0, traded=10.0, live=0.85, fills=2, n=1)
    rs = [dict(base, week=W + i * DAY, hold=1) for i in range(9)]
    wk = xb.weekly(rs)
    assert [r["n"] for r in wk] == [7, 2] and wk[0]["paper"] == pytest.approx(7.0) and wk[0]["fills"] == 14
    assert "7구간" in xb.fmt_compare(wk[0]) and xb.weekly([dict(base, week=W, hold=7)])[0]["n"] == 1


def test_halted_announces_once_per_turn_not_hourly(monkeypatch):
    """손실 한도로 정지된 동안 '리밸런스 안 함' 알림은 교체 차례마다 한 번 — 매시간 반복하지 않는다."""
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    rb(api, True, CFG, sig({"A": 1}, {"B": 1}))
    st = xb.load_state()
    st["halted"], st["halt_reason"] = True, "손실 한도"
    xb.save_state(st)
    n0 = len(api.orders)
    said = []
    monkeypatch.setattr(xb, "notify", lambda msg, cat=None, **k: said.append(msg))
    monkeypatch.setattr(xb, "bar_signal", lambda o: ({"A": 1.0}, {"B": 1.0}))
    t = NOW + 7 * DAY                                      # 다음 주 화 00:16 — 차례
    ts = [t + i * xb.H for i in range(5)] + [t + 7 * DAY + i * xb.H for i in range(3)]
    it = iter(ts)
    xb.loop(dict(CFG), api=api, sleep=lambda s: None, now=lambda: next(it), max_iter=len(ts))
    halted = [m for m in said if "정지 상태" in m]
    assert len(halted) == 2 and len(api.orders) == n0     # 5시간 동안 1번 + 다음 주 1번 · 주문 없음


def _cli(monkeypatch, api, *args):
    monkeypatch.setattr(xb.sys, "argv", ["xs_bot", *args])
    monkeypatch.setattr(xb.Api, "from_env", classmethod(lambda cls: api))
    monkeypatch.setattr("engine.env.load_dotenv", lambda *a, **k: 0)
    xb.main()


def test_rebase_sets_base_to_current_equity_and_notifies(monkeypatch):
    """--rebase = 기준을 지금 잔고로(수익을 지킬 때 등). 바뀐 기준·한도 금액을 알린다."""
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    rb(api, True, CFG, sig({"A": 1}, {"B": 1}))
    api.margin = 100.0
    said = []
    monkeypatch.setattr(xb, "notify", lambda m, *a, **k: said.append(m))
    _cli(monkeypatch, api, "--rebase")
    assert xb.load_state()["start_equity"] == 100.0 and "$30.00 → **$100.00**" in said[0] and "$70.00 아래" in said[0]
    api.margin = 65.0                                      # 새 기준 대비 −35%
    assert xb.check(api, 0.30) and xb.load_state()["halted"]


def _xfer(tid, amt, t, asset="USDT"):
    return dict(incomeType="TRANSFER", income=str(amt), asset=asset, time=t, tranId=tid, symbol="")


def test_deposit_is_detected_and_raises_base(monkeypatch):
    """입금이 자동으로 기준에 더해진다 → 한도가 새 잔고 기준으로 걸린다. 같은 이체는 두 번 세지 않는다."""
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    rb(api, True, CFG, sig({"A": 1}, {"B": 1}))            # 기준 $30 · 이체 기록 시작점 = NOW
    said = []
    monkeypatch.setattr(xb, "notify", lambda m, *a, **k: said.append(m))
    api.income = [_xfer(7, 70, NOW + 3_600_000)]
    api.margin = 100.0
    assert xb.check(api, 0.30, now_ms=NOW + 2 * 3_600_000) is None
    assert xb.load_state()["start_equity"] == 100.0 and "+70.00" in said[0] and "**$100.00**" in said[0]
    assert xb.check(api, 0.30, now_ms=NOW + 3 * 3_600_000) is None
    assert xb.load_state()["start_equity"] == 100.0 and len(said) == 1          # 겹쳐 읽어도 tranId 로 한 번만
    api.margin = 65.0
    assert xb.check(api, 0.30, now_ms=NOW + 4 * 3_600_000) and xb.load_state()["halted"]


def test_withdrawal_lowers_base_instead_of_false_loss_halt(monkeypatch):
    """출금($12 / $30 = −40%)을 손실로 보고 청산하던 것 → 기준을 내리고 아무것도 안 판다. 늦게 잡힌 출금도(20초 뒤 재확인)."""
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    rb(api, True, CFG, sig({"A": 1}, {"B": 1}))
    api.orders.clear()
    api.margin = 18.0
    api.income = [_xfer(8, -12, NOW + 3_600_000)]
    assert xb.check(api, 0.30, now_ms=NOW + 2 * 3_600_000) is None
    st = xb.load_state()
    assert st["start_equity"] == 18.0 and not st["halted"] and api.orders == []
    # 이체 기록이 늦게 잡힌 경우: 첫 조회엔 없고 한도 직전 재확인 때 보인다
    late = []

    def call(method, path, params=None, signed=False, retries=3, _real=api.call):
        if path == "/fapi/v1/income" and params.get("incomeType") == "TRANSFER":
            late.append(1)
            return [_xfer(9, -9, NOW + 4 * 3_600_000)] if len(late) > 1 else []
        return _real(method, path, params, signed, retries)
    monkeypatch.setattr(api, "call", call)
    api.margin = 9.0                                       # $18 → $9: 출금 $9
    assert xb.check(api, 0.30, now_ms=NOW + 5 * 3_600_000) is None and api.orders == []
    assert xb.load_state()["start_equity"] == 9.0


def test_old_transfers_are_not_applied_retroactively_or_after_rebase(monkeypatch):
    """이 기능 전부터 돌던 상태(transfer_seen 없음) · --rebase 직후엔 이미 잔고에 든 이체를 다시 세지 않는다."""
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    rb(api, True, CFG, sig({"A": 1}, {"B": 1}))
    st = xb.load_state()
    st.pop("transfer_seen")
    xb.save_state(st)
    api.income = [_xfer(1, 50, NOW - 5 * 60_000)]           # 예전 이체(이미 기준에 들어 있음)
    assert xb.check(api, 0.30, now_ms=NOW + 60_000) is None
    assert xb.load_state()["start_equity"] == 30.0
    api.income.append(_xfer(2, 40, NOW + 10 * 60_000))
    api.margin = 70.0
    xb.rebase(api, CFG, now_ms=NOW + 11 * 60_000)          # 지금 잔고 $70 에 방금 이체 $40 이 이미 들어 있다
    st = xb.load_state()
    assert st["start_equity"] == 70.0 and "2" in st["transfer_ids"]
    assert xb.check(api, 0.30, now_ms=NOW + 20 * 60_000) is None and xb.load_state()["start_equity"] == 70.0


def test_non_usd_transfer_is_reported_not_counted(monkeypatch):
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    rb(api, True, CFG, sig({"A": 1}, {"B": 1}))
    said = []
    monkeypatch.setattr(xb, "notify", lambda m, *a, **k: said.append(m))
    api.income = [_xfer(5, 0.1, NOW + 60_000, asset="BNB"), _xfer(6, 10, NOW + 60_000)]
    xb.check(api, 0.30, now_ms=NOW + 120_000)
    assert xb.load_state()["start_equity"] == 40.0 and "BNB 0.1" in said[0]


# ── 디스코드 명령(xs_cmd) ──
def test_cmd_queue_roundtrip_expiry_and_bad_file():
    from engine import xs_cmd
    xs_cmd.request("compare", who="me", now_ms=NOW)
    xs_cmd.request("flatten", who="me", now_ms=NOW - 11 * 60_000)
    with open(os.path.join(xs_cmd.cmd_dir(), "0-bad.json"), "w") as f:
        f.write("{not json")
    with pytest.raises(ValueError):
        xs_cmd.request("rm -rf")
    got = xs_cmd.take(now_ms=NOW)
    assert [(r.get("cmd"), r["expired"], bool(r.get("error"))) for r in got] == [
        (None, False, True), ("flatten", True, False), ("compare", False, False)]
    assert xs_cmd.take(now_ms=NOW) == []                   # 꺼내면 지워진다 — 두 번 실행 안 됨


def test_run_cmds_flatten_resume_go_rebase_and_expired(monkeypatch):
    from engine import xs_cmd
    api = FakeApi({"A": 1.0, "B": 1.0, "X": 1.0}, pos={"X": 5.0}, margin=30.0)
    rb(api, True, CFG, sig({"A": 1}, {"B": 1}))
    said = []
    monkeypatch.setattr(xb, "notify", lambda m, *a, **k: said.append(m))
    monkeypatch.setattr(xb, "bar_signal", lambda o: ({"A": 1.0}, {"B": 1.0}))
    t = NOW + 3_600_000
    xs_cmd.request("flatten", who="me", now_ms=t)
    assert xb.run_cmds(api, CFG, t) == ["flatten"]
    st = xb.load_state()
    assert st["halted"] and st["positions"] == {} and api.pos == {"X": 5.0} and "청산 2개" in said[-1]
    xs_cmd.request("resume", who="me", now_ms=t - 11 * 60_000)    # 만료 — 실행 안 함
    assert xb.run_cmds(api, CFG, t) == [] and xb.load_state()["halted"] and "만료" in said[-1]
    xs_cmd.request("resume_go", who="me", now_ms=t)
    api.orders.clear()
    assert xb.run_cmds(api, CFG, t) == ["resume_go"]
    st = xb.load_state()
    assert not st["halted"] and set(st["positions"]) == {"A", "B"} and api.orders and "진입 done" in said[-1]
    api.margin = 33.0
    xs_cmd.request("rebase", who="me", now_ms=t)
    xb.run_cmds(api, CFG, t)
    assert xb.load_state()["start_equity"] == 33.0
    lines = open(os.path.join(os.environ["XS_DIR"], "cmd_log.jsonl")).read().splitlines()
    assert len(lines) == 4


def test_run_cmds_resume_go_in_plan_mode_does_not_order(monkeypatch):
    from engine import xs_cmd
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    xs_cmd.request("resume_go", who="me", now_ms=NOW)
    said = []
    monkeypatch.setattr(xb, "notify", lambda m, *a, **k: said.append(m))
    xb.run_cmds(api, dict(CFG, mode="plan"), NOW)
    assert api.orders == [] and "진입 안 함" in said[-1]


def test_loop_runs_discord_command_and_halt_alert_has_buttons(monkeypatch):
    from engine import xs_cmd
    api = FakeApi({"A": 1.0, "B": 1.0}, margin=30.0)
    sent = []
    monkeypatch.setattr(xb, "notify", lambda m, *a, **k: sent.append((m, k.get("buttons"))))
    t = _ms(2026, 11, 17, 0, 16)
    xs_cmd.request("split_on", who="me", now_ms=t)
    _run_loop(monkeypatch, api, "live", [t])
    assert xb.split_requested() and any("/xs 요일 분산" in m for m, _ in sent)
    api.margin = 20.0                                      # −33% → 손실 한도 알림에 [대조][재개]
    _run_loop(monkeypatch, api, "live", [t + 2 * 3_600_000])
    halt = [b for m, b in sent if m.startswith("⛔ XS MOM 손실 한도")]
    assert halt and halt[0] == ["xs_compare", "xs_resume"]


def test_status_text_shows_base_limit_and_halt():
    from engine import xs_cmd
    txt = xs_cmd.status_text(dict(mode="live", gross=2, halted=True, halt_reason="손실 한도 −31%", equity=120.0,
                                  start_equity=174.45, pnl_pct=-0.312, max_loss=0.30, long_usd=0, short_usd=0, upnl=0,
                                  positions=[], schedule="월요일 하나(주 1회)", next_rebalance="2026-12-01 00:15 UTC",
                                  updated="x"))
    assert "정지" in txt and "$122.11 아래" in txt and "−31.2%" not in txt and "-31.2%" in txt
    assert xs_cmd.status_text({"error": "없음"}).startswith("⚠️")
