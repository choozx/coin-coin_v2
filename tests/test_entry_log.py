"""진입 판정 로그 — 조건 설명·지표 스냅샷·차단 사유. 네트워크 없음.

배경: 봇이 조용히 아무것도 안 할 때 그게 조건 미충족인지 멈춤인지 쿨다운인지 워밍업인지
밖에서 알 수 없었다. 상태 파일은 '무포지션'만 알려주고 이유를 안 남긴다.

가장 중요한 불변식: **explain 은 evaluate 와 결론이 같아야 한다.** 로그가 판정과 다르면
없느니만 못하다 — 사람이 로그를 믿고 엉뚱한 데를 고치게 된다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import conditions as C            # noqa: E402
from engine import entry_log                  # noqa: E402
from engine.candles import Candles            # noqa: E402


def _candles(n=300):
    """단조 상승 + 잔물결 — 지표가 워밍업을 마치고 실제 값을 갖도록 충분히 길게."""
    t = np.arange(n, dtype=float)
    close = 100.0 + t * 0.1 + np.sin(t / 3.0) * 2.0
    return Candles(open_time=(np.arange(n) * 60_000).astype(np.int64),
                   open=close, high=close + 1.0, low=close - 1.0,
                   close=close, volume=np.full(n, 10.0), timeframe_min=1)


def _resolver():
    return C.SeriesResolver(_candles())


RSI = {"indicator": "RSI", "period": 14}
EMA = {"indicator": "EMA", "period": 20}
CLOSE = {"source": "close"}


# ---- 핵심 불변식 ----

def test_explain_agrees_with_evaluate():
    """모든 노드 형태에서 explain.ok == evaluate. 이게 깨지면 로그가 거짓말을 한다."""
    r, i = _resolver(), 250
    nodes = [
        {"left": RSI, "cmp": "<", "right": 30},
        {"left": RSI, "cmp": ">", "right": 30},
        {"left": CLOSE, "cmp": ">", "right": EMA},
        {"cross": "crossOver", "left": CLOSE, "right": EMA},
        {"cross": "crossUnder", "left": CLOSE, "right": EMA},
        {"op": "AND", "children": [{"left": RSI, "cmp": "<", "right": 30},
                                   {"left": CLOSE, "cmp": ">", "right": EMA}]},
        {"op": "OR", "children": [{"left": RSI, "cmp": "<", "right": 30},
                                  {"left": CLOSE, "cmp": ">", "right": EMA}]},
        {"op": "NOT", "children": [{"left": RSI, "cmp": "<", "right": 30}]},
    ]
    for node in nodes:
        assert C.explain(node, r, i)["ok"] == C.evaluate(node, r, i), node


def test_explain_agrees_during_warmup():
    """워밍업(NaN) 구간에서도 일치해야 한다 — evaluate 는 NaN 을 false 로 본다."""
    r = _resolver()
    node = {"op": "AND", "children": [{"left": RSI, "cmp": "<", "right": 30},
                                      {"left": CLOSE, "cmp": ">", "right": EMA}]}
    for i in (0, 1, 5, 13, 19):
        assert C.explain(node, r, i)["ok"] == C.evaluate(node, r, i) is False


# ---- 읽을 수 있는가 ----

def test_constant_is_not_labelled_twice():
    """상수는 '30' 이지 '30=30' 이 아니다."""
    text = C.explain({"left": RSI, "cmp": "<", "right": 30}, _resolver(), 250)["text"]
    assert "30=30" not in text and text.endswith("< 30")
    assert text.startswith("RSI(14)=")


def test_nan_shows_warmup_once():
    """NaN 은 값 자리에 한 번만 적는다(꼬리표 중복 금지)."""
    text = C.explain({"left": RSI, "cmp": "<", "right": 30}, _resolver(), 2)["text"]
    assert text.count("워밍업") == 1


def test_indicator_snapshot_skips_constants():
    """스냅샷엔 지표·시세만 — 상수 30 을 '현재값'이라 적을 이유가 없다."""
    node = {"op": "AND", "children": [{"left": RSI, "cmp": "<", "right": 30},
                                      {"left": CLOSE, "cmp": ">", "right": EMA}]}
    snap = C.indicator_snapshot(node, _resolver(), 250)
    assert set(snap) == {"RSI(14)", "종가", "EMA(20)"}
    assert all(v is not None for v in snap.values())


def test_explain_lines_are_indented_by_depth():
    node = {"op": "AND", "children": [{"left": RSI, "cmp": "<", "right": 30}]}
    lines = C.explain_lines(C.explain(node, _resolver(), 250))
    assert lines[0].startswith(("✓", "✗")) and lines[1].startswith("  ")


# ---- 기록 ----

class _P:
    name, symbol, timeframe, direction = "t", "BTCUSDT", "15m", "long"
    entry = {"left": RSI, "cmp": "<", "right": 30}


def test_build_record_has_reason_conditions_and_indicators():
    """요청된 세 가지가 다 들어있는가: 조건 / 지표 현황 / 안 되는 이유."""
    rec = entry_log.build(_P(), None, _resolver(), 250, 1_000, 2_000,
                          None, "진입 조건 미충족", 61234.5, decided=True)
    assert rec["block"] == "진입 조건 미충족" and rec["entered"] is None
    assert rec["rules"][0]["side"] == "long" and rec["rules"][0]["lines"]
    assert "RSI(14)" in rec["indicators"]
    assert rec["decided"] is True and rec["bar"] == 1_000 and rec["price"] == 61234.5


def test_build_covers_both_sides_of_entry_rules():
    """롱/숏 그룹이 따로 있으면 둘 다 남긴다 — 한쪽만 보면 왜 숏도 안 났는지 모른다."""
    rules = [{"side": "long", "when": {"left": RSI, "cmp": "<", "right": 30}},
             {"side": "short", "when": {"left": RSI, "cmp": ">", "right": 70}}]
    rec = entry_log.build(_P(), rules, _resolver(), 250, 1, 2, None, "진입 조건 미충족", 1.0, False)
    assert [r["side"] for r in rec["rules"]] == ["long", "short"]


def test_summary_says_entered_or_why_not():
    entered = entry_log.build(_P(), None, _resolver(), 250, 1, 2, 1, None, 1.0, True)
    assert entry_log.summary(entered) == "진입✓ 롱"
    blocked = entry_log.build(_P(), None, _resolver(), 250, 1, 2, None, "포지션 보유 중", 1.0, False)
    assert "진입✗" in entry_log.summary(blocked) and "포지션 보유 중" in entry_log.summary(blocked)


def test_append_and_tail_roundtrip():
    path = os.path.join(tempfile.mkdtemp(), "e.jsonl")
    for k in range(5):
        entry_log.append({"at": k}, path, keep=0)
    assert [r["at"] for r in entry_log.tail(path, 3)] == [2, 3, 4]


def test_append_trims_to_keep():
    """저사양 EC2 다 — 로그가 무한히 자라면 안 된다. keep*1.5 를 넘으면 오래된 줄을 버린다."""
    path = os.path.join(tempfile.mkdtemp(), "e.jsonl")
    for k in range(400):
        entry_log.append({"at": k}, path, keep=100)
    with open(path, encoding="utf-8") as f:
        n = sum(1 for _ in f)
    assert n <= 150                                  # keep(100) 의 1.5배 이내로 유지
    assert json.loads(open(path, encoding="utf-8").readlines()[-1])["at"] == 399   # 최신은 남는다


def test_append_never_raises():
    """기록은 관찰용이다 — 실패해도 매매 경로를 멈추면 안 된다."""
    entry_log.append({"at": 1}, "/nonexistent-root-dir/x/e.jsonl", keep=10)   # 예외 없이 통과
    entry_log.append({"at": 1}, "", keep=10)                                   # 기록 끔
    assert entry_log.tail("/nope/none.jsonl") == []


# ---- 어떤 조건이 안 맞는가 ----

def test_failed_leaves_names_only_the_blocking_conditions():
    """논리 노드('모두 1/2 충족')는 요약이지 원인이 아니다 — 말단 조건만 원인으로 센다."""
    r = _resolver()
    node = {"op": "AND", "children": [{"left": RSI, "cmp": ">", "right": -1},    # 참
                                      {"left": RSI, "cmp": "<", "right": -1}]}   # 거짓
    fails = C.failed_leaves(C.explain(node, r, 250))
    assert len(fails) == 1 and fails[0].startswith("RSI(14)=") and "< -1" in fails[0]


def test_failed_leaves_empty_when_all_met():
    r = _resolver()
    assert C.failed_leaves(C.explain({"left": RSI, "cmp": ">", "right": -1}, r, 250)) == []


def test_failed_leaves_lists_every_branch_of_a_failed_or():
    """OR 은 하나만 참이면 되지만, 전부 거짓이면 전부가 원인이다."""
    r = _resolver()
    node = {"op": "OR", "children": [{"left": RSI, "cmp": "<", "right": -1},
                                     {"left": RSI, "cmp": "<", "right": -2}]}
    assert len(C.failed_leaves(C.explain(node, r, 250))) == 2


def test_summary_names_the_failing_condition_with_its_value():
    """'조건 미충족' 네 글자만 매분 반복하면 정보가 0이다 — 무엇이 얼마인지 적어야 한다."""
    rules = [{"side": "long", "when": {"op": "AND", "children": [
        {"left": RSI, "cmp": ">", "right": -1},          # 충족
        {"left": RSI, "cmp": "<", "right": -1}]}}]       # 미충족
    rec = entry_log.build(_P(), rules, _resolver(), 250, 1, 2, None, "진입 조건 미충족", 1.0, False)
    line = entry_log.summary(rec)
    assert "long ✗ RSI(14)=" in line and "< -1" in line
    assert rec["rules"][0]["failed"] == C.failed_leaves(
        C.explain(rules[0]["when"], _resolver(), 250))


def test_summary_caps_the_list_and_says_how_many_more():
    """조건이 많으면 줄이 길어진다 — 앞 2개만 이름 대고 나머지는 개수로."""
    conds = [{"left": RSI, "cmp": "<", "right": -k} for k in range(1, 6)]
    rules = [{"side": "long", "when": {"op": "AND", "children": conds}}]
    rec = entry_log.build(_P(), rules, _resolver(), 250, 1, 2, None, "진입 조건 미충족", 1.0, False)
    assert "외 3개" in entry_log.summary(rec)


def test_summary_leaves_self_explanatory_reasons_alone():
    """쿨다운·멈춤 같은 사유는 그 자체로 완결이다 — 조건 목록을 덧붙이지 않는다."""
    rec = entry_log.build(_P(), None, _resolver(), 250, 1, 2, None, "청산 쿨다운 (1/2봉 경과)", 1.0, True)
    assert entry_log.summary(rec) == "진입✗ [판정] 청산 쿨다운 (1/2봉 경과)"


def test_indicator_label_uses_values_not_param_names():
    """SUPERTREND_DIR(10,3.0) — 차트 표기와 같게. 매분 찍히는 줄이라 짧아야 한다."""
    st = {"indicator": "SUPERTREND_DIR", "period": 10, "params": {"multiplier": 3.0}}
    assert C.operand_label(st) == "SUPERTREND_DIR(10,3.0)"
