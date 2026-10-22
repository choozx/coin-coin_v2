"""영상 분석 '🛠 구현 가능' 2차 — 엔진 확장. 네트워크 없음.

MA_DIST(이평 괴리 ATR) · RETRACE_UP/DN(직전 파동 되돌림 %) · TRENDLINE_HIGH/LOW(두 피벗을 이은 선)
· params.htf(상위 TF 의 닫힌 봉 값) · operand.symbol(다른 코인) · distNode((A − B) ÷ ATR 비교)
· takeProfit/breakeven retrace(파동 n% 되돌림 가격) · entryRules[].sizeScale(규칙별 비중).
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import indicators as ind               # noqa: E402
from engine import ranking, server, video_analyze  # noqa: E402
from engine.backtest import BacktestConfig, run    # noqa: E402
from engine.candles import Candles, resample       # noqa: E402
from engine.conditions import (SeriesResolver, evaluate, explain, operand_label,  # noqa: E402
                               referenced_symbols, required_warmup_bars)
from engine.preset import Preset                   # noqa: E402

M = 60_000


def _candles(n=1500, seed=5, vol=0.002, t0=0):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, vol, n)))
    op = np.r_[close[0], close[:-1]]
    high = np.maximum(op, close) * (1 + rng.uniform(0, 0.002, n))
    low = np.minimum(op, close) * (1 - rng.uniform(0, 0.002, n))
    return Candles(open_time=t0 + np.arange(n, dtype=np.int64) * M, open=op, high=high, low=low,
                   close=close, volume=rng.uniform(5, 50, n), timeframe_min=1)


def _cut(c: Candles, k: int) -> Candles:
    return Candles(open_time=c.open_time[:k], open=c.open[:k], high=c.high[:k], low=c.low[:k],
                   close=c.close[:k], volume=c.volume[:k], timeframe_min=c.timeframe_min)


def _loader_from(c: Candles, mult=2.0):
    """다른 심볼 = 같은 시각 · 가격 mult 배인 가짜 코인."""
    def load(sym, s, e):
        m = (c.open_time >= s) & (c.open_time <= e)
        return Candles(c.open_time[m], c.open[m] * mult, c.high[m] * mult, c.low[m] * mult, c.close[m] * mult,
                       c.volume[m], 1)
    return load


OPERANDS = [
    {"indicator": "MA_DIST", "period": 22, "params": {"sma": 1}},
    {"indicator": "MA_DIST", "period": 20, "params": {"sma": 0, "atr": 10}},
    {"indicator": "RETRACE_UP", "period": 40}, {"indicator": "RETRACE_DN", "period": 40},
    {"indicator": "TRENDLINE_HIGH", "period": 3}, {"indicator": "TRENDLINE_LOW", "period": 2},
    {"indicator": "EMA", "period": 5, "params": {"htf": 60}},
    {"indicator": "RSI", "period": 7, "params": {"htf": 15}},
]


@pytest.mark.parametrize("op", OPERANDS, ids=lambda o: o["indicator"] + str(o.get("params", "")))
def test_causal(op):
    """뒤에 봉을 붙여도 과거 값이 안 바뀐다 — 미래를 안 본다(상위 TF 는 덜 닫힌 봉을 안 쓴다)."""
    c = _candles()
    full = SeriesResolver(c).resolve(op)
    for k in (300, 777, 1201):
        part = SeriesResolver(_cut(c, k)).resolve(op)
        np.testing.assert_allclose(part, full[:k], equal_nan=True)


def test_causal_dist_node():
    c = _candles()
    node = {"dist": "absAtr", "left": {"indicator": "EMA", "period": 9},
            "right": {"indicator": "EMA", "period": 9, "params": {"htf": 30}}, "cmp": "<", "value": 0.5}
    full = [evaluate(node, SeriesResolver(c), i) for i in range(len(c))]
    part = [evaluate(node, SeriesResolver(_cut(c, 900)), i) for i in range(900)]
    assert full[:900] == part and any(full) and not all(full[100:])


def test_ma_dist_matches_formula():
    c = _candles()
    v = ind.ma_dist(c.high, c.low, c.close, 22, True, 14)
    want = (c.close - ind.sma(c.close, 22)) / ind.atr(c.high, c.low, c.close, 14)
    np.testing.assert_allclose(v, want, equal_nan=True)


def test_retrace_brute():
    c = _candles(600)
    n = 30
    up, dn = ind.retrace(c.high, c.low, c.close, n, True), ind.retrace(c.high, c.low, c.close, n, False)
    assert np.isnan(up[:n - 1]).all()
    for t in range(n - 1, len(c), 7):
        h, lo = c.high[t - n + 1:t + 1], c.low[t - n + 1:t + 1]
        k = int(h.argmax()); L = lo[k:].min()
        assert up[t] == pytest.approx((c.close[t] - L) / (h[k] - L) * 100)
        k = int(lo.argmin()); H = h[k:].max()
        assert dn[t] == pytest.approx((H - c.close[t]) / (H - lo[k]) * 100)
        assert ind.retrace_price(c.high, c.low, t, n, 0.25, True) == pytest.approx(lo[int(h.argmax()):].min()
                                                                                + 0.25 * (h.max() - lo[int(h.argmax()):].min()))


def test_pivot_line_brute():
    c = _candles(800)
    n = 3
    line = ind.pivot_line(c.high, c.low, n, True)
    _, highs = ind.fractal_points(c.high, c.low, n)
    for t in range(0, len(c), 13):
        conf = highs[highs + n <= t]                      # 봉 t 에서 확정된 고점들
        if len(conf) < 2:
            assert np.isnan(line[t]); continue
        i0, i1 = conf[-2], conf[-1]
        want = c.high[i1] + (c.high[i1] - c.high[i0]) / (i1 - i0) * (t - i1)
        assert line[t] == pytest.approx(want)


def test_htf_uses_only_closed_bars():
    c = _candles(3000)
    v = SeriesResolver(c).resolve({"indicator": "EMA", "period": 5, "params": {"htf": 60}})
    e = ind.ema(resample(c, 60).close, 5)
    # 1분봉 599(599~600분에 닫힘) = 1시간봉 9(540~599분) 가 막 닫힘 → e[9]. 598 은 아직 e[8]
    assert v[599] == pytest.approx(e[9]) and v[598] == pytest.approx(e[8]) and v[540] == pytest.approx(e[8])
    # 5분봉 신호 위에서도 같은 원리
    s5 = resample(c, 5)
    v5 = SeriesResolver(s5).resolve({"indicator": "EMA", "period": 5, "params": {"htf": 60}})
    assert v5[119] == pytest.approx(e[9]) and v5[118] == pytest.approx(e[8])


def test_htf_must_be_multiple():
    with pytest.raises(ValueError):
        SeriesResolver(resample(_candles(600), 5)).resolve({"indicator": "EMA", "period": 5, "params": {"htf": 7}})


def test_warmup_scales_with_htf():
    node = {"left": {"source": "close"}, "cmp": ">", "right": {"indicator": "EMA", "period": 21, "params": {"htf": 1440}}}
    assert required_warmup_bars(node, tf_min=5) == 21 * 288 * 3
    assert required_warmup_bars(node) == 200                 # tf 모르면 예전 그대로


def test_other_symbol_aligned_and_incomplete_bucket_nan():
    c = _candles(1000)
    sig = resample(c, 5)
    r = SeriesResolver(sig, "ALTUSDT", loader=_loader_from(c))
    a = r.resolve({"indicator": "SMA", "period": 3, "symbol": "BTCUSDT"})
    b = r.resolve({"indicator": "SMA", "period": 3})
    np.testing.assert_allclose(a, 2 * b, equal_nan=True)
    px = r.resolve({"source": "close", "symbol": "BTCUSDT"})
    np.testing.assert_allclose(px, 2 * sig.close)
    # 그 심볼의 마지막 신호봉 구간이 덜 찼으면(1분봉 빠짐) 그 봉은 NaN — 덜 닫힌 봉으로 판정하지 않는다
    short = _cut(c, len(c) - 2)
    r2 = SeriesResolver(sig, "ALTUSDT", loader=_loader_from(short))
    px2 = r2.resolve({"source": "close", "symbol": "BTCUSDT"})
    assert np.isnan(px2[-1]) and px2[-2] == pytest.approx(2 * sig.close[-2])
    # 데이터가 아예 없으면 전부 NaN(= 조건 거짓)
    r3 = SeriesResolver(sig, "ALTUSDT", loader=lambda *a: None)
    assert np.isnan(r3.resolve({"source": "close", "symbol": "BTCUSDT"})).all()


def test_same_symbol_operand_is_plain():
    c = _candles(300)
    r = SeriesResolver(c, "BTCUSDT", loader=lambda *a: pytest.fail("자기 심볼은 다시 읽지 않는다"))
    np.testing.assert_allclose(r.resolve({"source": "close", "symbol": "BTCUSDT"}), c.close)


def test_referenced_symbols_and_labels():
    node = {"op": "AND", "children": [
        {"left": {"source": "close", "symbol": "BTCUSDT"}, "cmp": ">", "right": {"indicator": "EMA", "period": 21, "symbol": "BTCUSDT"}},
        {"left": {"source": "close"}, "cmp": ">", "right": {"indicator": "EMA", "period": 21, "params": {"htf": 1440}}}]}
    assert referenced_symbols(node) == {"BTCUSDT"}
    assert operand_label({"indicator": "EMA", "period": 21, "params": {"htf": 1440}}) == "EMA(21)@1d"
    assert operand_label({"source": "close", "symbol": "BTCUSDT"}) == "BTCUSDT·종가"


def test_dist_node_evaluate_and_explain_agree():
    c = _candles()
    r = SeriesResolver(c)
    node = {"dist": "atr", "left": {"source": "high"}, "right": {"indicator": "PREV_HIGH", "period": 1}, "cmp": ">=", "value": 0.3}
    d = (c.high - ind.prev_extreme(c.high, 1, True)) / ind.atr(c.high, c.low, c.close, 14)
    hits = 0
    for i in range(len(c)):
        want = bool(not np.isnan(d[i]) and d[i] >= 0.3)
        assert evaluate(node, r, i) == want == explain(node, r, i)["ok"]
        hits += want
    assert 0 < hits < len(c)
    assert "÷ATR" in explain(node, r, 500)["text"]


# ---- 프리셋·백테스트 ----
def _form(**kw):
    p = {"symbol": "BTCUSDT", "timeframe": "1m", "days": "1", "equity": "10000", "slType": "percent", "slValue": "5",
         "tpType": "off", "leverage": "1", "sizeType": "equityPercent", "sizeValue": "10", "cooldownBars": "100",
         "avoidFundingMin": "0", "entryType": "taker", "levTiers": [],
         "entryGroups": [{"side": "long", "conditions": [{"cross": "crossOver", "left": {"source": "close"}, "right": 95.5}]}]}
    p.update(kw)
    return p


def _seq(prices):
    cl = np.asarray(prices, float)
    return Candles(open_time=np.arange(len(cl), dtype=np.int64) * M, open=cl.copy(), high=cl + 0.05, low=cl - 0.05,
                   close=cl, volume=np.full(len(cl), 10.0), timeframe_min=1)


def _bt(prices, **kw):
    preset = Preset.from_dict(server._build_preset(_form(**kw)), validate=True)
    return run(_seq(prices), preset, BacktestConfig(initial_equity=10000.0, taker_fee=0.0, maker_fee=0.0))


# 100 에서 90 까지 빠졌다가 반등 — 95.5 상향 교차에서 롱. 파동 H=100.05, L=89.95 → 25% = 92.475(진입가 아래 = 무효), 75% = 97.525
_WAVE = [100.0] * 3 + list(np.linspace(99, 90, 10)) + [92.0, 94.0, 95.0, 96.0, 96.5, 97.0, 97.6, 98.0, 97.0]


def test_retrace_take_profit_price():
    m = _bt(_WAVE, tpType="retrace", tpValue="75", tpLookback="30")
    tr = m.trades[0]
    assert tr.exit_reason == "take_profit" and tr.exit_price == pytest.approx(89.95 + 0.75 * (100.05 - 89.95))
    # 되돌림 가격이 진입가보다 불리하면(25%) 익절 없음 → 마지막 봉까지 들고 간다
    assert _bt(_WAVE, tpType="retrace", tpValue="25", tpLookback="30").trades[0].exit_reason == "signal"


def test_retrace_breakeven_trigger():
    path = _WAVE[:-3] + [97.0, 95.8, 95.0]                 # 97.0 에서 70% 되돌림 트리거 터치 후 진입가 아래로
    plain = _bt(path)
    assert plain.trades[0].exit_reason == "signal"
    be = _bt(path, beEnabled=True, beType="retrace", beValue="65", beLookback="30")
    tr = be.trades[0]
    assert tr.exit_reason == "breakeven" and tr.exit_price == pytest.approx(tr.entry_price)
    p = server._build_preset(_form(beEnabled=True, beType="retrace", beValue="25", beLookback="40"))
    assert p["exit"]["breakeven"] == {"type": "retrace", "value": 25.0, "offsetPercent": 0.0, "lookback": 40}
    assert "파동 25% 되돌림 에 본전 손절" in str(ranking.describe(p))


def test_breakeven_state_survives_sidecar(tmp_path):
    """라이브 재시작: 본전 트리거·옮김 여부가 사이드카에 남는다."""
    import json
    from engine.backtest import _Position
    from types import SimpleNamespace
    from engine.executor import LiveExecutor
    ex = SimpleNamespace(position_path=str(tmp_path / "pos.json"), symbol="BTCUSDT")
    ex.position = _Position(side=1, entry_time=0, entry_price=100.0, qty=1.0, leverage=1, margin=100.0, liq_price=0.0,
                            stop_price=100.0, tp_price=float("nan"), entry_fee=0.0, entry_signal_time=0, peak=103.0,
                            be_moved=True, be_trigger=102.5)
    LiveExecutor._save_position(ex)                       # 거래소 없이 사이드카 기록만
    d = json.load(open(tmp_path / "pos.json"))
    assert d["beMoved"] is True and d["beTrigger"] == 102.5


def test_size_scale_per_rule():
    groups = [{"side": "long", "conditions": [{"cross": "crossOver", "left": {"source": "close"}, "right": 95.5}],
               "sizeScale": 3}]
    p = server._build_preset(_form(entryGroups=groups))
    assert p["entryRules"][0]["sizeScale"] == 3
    one, three = _bt(_WAVE), _bt(_WAVE, entryGroups=groups)
    assert three.trades[0].qty == pytest.approx(3 * one.trades[0].qty)
    assert "롱×3" in ranking.describe(p)["entry"]
    assert "sizeScale" not in server._build_preset(_form())["entryRules"][0]


def test_sweep_path_into_list():
    """최적화 오버라이드 경로가 AND 노드 안(children.0…)도 찾아간다."""
    node = {"op": "AND", "children": [{"left": {"indicator": "RSI", "period": 14}, "cmp": "<", "right": 30},
                                      {"left": {"source": "close"}, "cmp": ">", "right": 1}]}
    p = server._build_preset(_form(entryGroups=[{"side": "long", "conditions": [node]}],
                                   **{"@entry:0:0:children.0.right": 25}))
    assert p["entryRules"][0]["when"]["children"][0]["right"] == 25


# 화면 COND_TYPES 가 만드는 노드 모양 그대로
GUI_NODES = [
    {"left": {"indicator": "MA_DIST", "period": 22, "params": {"sma": 1}}, "cmp": "<", "right": -2},
    {"left": {"indicator": "RETRACE_UP", "period": 50}, "cmp": ">", "right": 75},
    {"cross": "crossOver", "left": {"source": "close"}, "right": {"indicator": "TRENDLINE_HIGH", "period": 3}},
    {"left": {"source": "close"}, "cmp": ">", "right": {"indicator": "EMA", "period": 21, "params": {"htf": 60}}},
    {"dist": "absAtr", "left": {"indicator": "EMA", "period": 21}, "right": {"indicator": "EMA", "period": 21, "params": {"htf": 60}},
     "cmp": "<", "value": 0.5},
    {"dist": "atr", "left": {"source": "high"}, "right": {"indicator": "PREV_HIGH", "period": 1}, "cmp": ">=", "value": 0.3},
    {"left": {"source": "close", "symbol": "ETHUSDT"}, "cmp": ">", "right": {"indicator": "EMA", "period": 21, "symbol": "ETHUSDT"}},
    {"left": {"indicator": "PIVOT_HIGH", "period": 5, "params": {"nth": 0}}, "cmp": "<",
     "right": {"indicator": "PIVOT_HIGH", "period": 5, "params": {"nth": 1}}},
]


@pytest.mark.parametrize("node", GUI_NODES, ids=range(len(GUI_NODES)))
def test_gui_nodes_validate_and_run(node, monkeypatch):
    c = _candles(3000, vol=0.003)
    from engine import conditions
    monkeypatch.setattr(conditions, "_load_other", _loader_from(c))
    preset = Preset.from_dict(server._build_preset(_form(entryGroups=[{"side": "long", "conditions": [node]}])), validate=True)
    run(c, preset, BacktestConfig(initial_equity=10000.0))
    ranking.describe(preset.data)


def test_schema_rejects_bad_dist():
    bad = {"dist": "pct", "left": {"source": "high"}, "right": {"source": "low"}, "cmp": ">", "value": 1}
    with pytest.raises(Exception):
        Preset.from_dict(server._build_preset(_form(entryGroups=[{"side": "long", "conditions": [bad]}])), validate=True)


def test_draft_new_fields():
    """영상 초안: 거리 조건 · 다른 코인 · 상위 TF · 그룹 비중 · 되돌림 익절/본전 → 화면 폼 + 검증 통과."""
    num = lambda v: {"kind": "number", "value": v}
    src = lambda s, sym="": {"kind": "source", "source": s, "symbol": sym}
    indi = lambda name, per=0, pj="", sym="": {"kind": "indicator", "indicator": name, "period": per, "params_json": pj, "symbol": sym}
    draft = {
        "name": "t", "timeframe": "5m",
        "groups": [{"side": "long", "size_scale": 3, "conds": [
            {"left": src("high"), "op": ">=", "right": indi("PREV_HIGH", 1), "dist": "atr", "dist_value": 0.3, "note": "훨씬 높음"},
            {"left": src("close", "BTCUSDT"), "op": ">", "right": indi("EMA", 21, sym="btcusdt"), "dist": "none", "note": "BTC 필터"},
            {"left": src("close"), "op": "crossOver", "right": indi("EMA", 21, '{"htf":1440}'), "note": "일봉선"}]}],
        "stop": {"type": "swing", "lookback": 20}, "take": {"type": "retrace", "value": 50, "lookback": 60},
        "exit_long": [], "exit_short": [], "time_stop_bars": 0,
        "breakeven": {"type": "retrace", "value": 25, "lookback": 60},
        "assumed": [], "excluded": []}
    form, params = video_analyze._check_draft(draft)
    g = form["entry"][0]
    assert g["sizeScale"] == 3
    assert g["conds"][0]["type"] == "customDist" and g["conds"][0]["k"] == "0.3"
    assert g["conds"][1]["lsym"] == "BTCUSDT" and g["conds"][1]["rsym"] == "BTCUSDT"
    assert form["tpType"] == "retrace" and form["tpLookback"] == "60" and form["beType"] == "retrace"
    p = server._build_preset(params)
    when = p["entryRules"][0]["when"]["children"]
    assert when[0] == {"dist": "atr", "left": {"source": "high"}, "right": {"indicator": "PREV_HIGH", "period": 1},
                       "cmp": ">=", "value": 0.3}
    assert when[1]["right"]["symbol"] == "BTCUSDT" and when[2]["right"]["params"] == {"htf": 1440}
    assert p["exit"]["takeProfit"] == {"type": "retrace", "value": 50.0, "lookback": 60}


def test_chat_marks_old_engine_conversation():
    """엔진이 바뀌기 전 대화면 화면에 표시하고, 다음 턴 프롬프트에 '예전 기준'이라고 알려 준다."""
    st = {"messages": [{"role": "user", "text": "초안", "at": 0}, {"role": "assistant", "text": "HTF 없음", "at": 0}],
          "draft": None, "draftError": None}
    assert video_analyze._chat_stale(st)                      # 엔진 표시 없는 예전 대화
    meta = {"title": "t", "channel": "c", "description": "", "transcript": "x"}
    prompt = video_analyze._chat_prompt(meta, {"strategies": []}, st, "다시")
    assert "엔진에 기능이 추가되기 전" in prompt
    st["engine"] = video_analyze.engine_hash()
    assert not video_analyze._chat_stale(st)
    assert "엔진에 기능이 추가되기 전" not in video_analyze._chat_prompt(meta, {"strategies": []}, st, "다시")
    assert not video_analyze._chat_stale({"messages": []})    # 빈 대화는 표시 안 함
