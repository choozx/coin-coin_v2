"""백테스트 'AI 최적화' — AI 가 고른 범위 검증, 한 번 고쳐 묻기, 프리셋별 누적 조합으로 귀무 게이트. Claude 는 가짜.

핵심 약속: AI 최적화를 같은 프리셋에 반복하면 판정 N 이 누적돼 기준이 올라간다(다시 돌려 운으로 통과하기 방지).
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import ai_optimize as ai               # noqa: E402
from engine import server                          # noqa: E402
from engine.candles import Candles                 # noqa: E402

CANDS = [{"key": "@entry:0:0:left.period", "label": "RSI 기간", "value": "14", "def": [7, 21, 7], "int": True},
         {"key": "@entry:0:0:right", "label": "RSI 기준", "value": "30", "def": [20, 40, 5], "int": True},
         {"key": "tpValue", "label": "익절 값", "value": "2", "def": [0.5, 3, 0.5], "int": False}]
FORM = {"symbol": "BTCUSDT", "timeframe": "5m", "days": "30", "equity": "10000", "slType": "percent", "slValue": "2",
        "tpType": "percent", "tpValue": "2", "leverage": "1", "sizeType": "equityPercent", "sizeValue": "10",
        "cooldownBars": "0", "avoidFundingMin": "0", "entryType": "taker", "levTiers": [],
        "entryGroups": [{"side": "long", "conditions": [{"cross": "crossOver", "left": {"indicator": "RSI", "period": 14},
                                                          "right": 30.0}]}]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(ai, "LEDGER", str(tmp_path / "ai_optimize.json"))
    monkeypatch.setattr(ai, "VERDICTS", str(tmp_path / "ai_optimize_verdicts.jsonl"))
    calls = []

    def fake(replies):
        it = iter(replies)

        def _claude(prompt, schema, system, timeout=600):
            calls.append(prompt)
            return next(it), {"cost": 0.01, "seconds": 1.0, "model": "x"}
        monkeypatch.setattr(ai, "_claude", _claude)
    return {"fake": fake, "calls": calls}


C = {c["key"]: c for c in CANDS}


@pytest.mark.parametrize("sweep, msg", [
    ([], "1~3"),
    ([{"key": "leverage", "min": 1, "max": 5, "step": 1}], "범위를 켤 수"),                 # 후보에 없음
    ([{"key": "@entry:0:0:left.period", "min": 7, "max": 21, "step": 0.5}], "정수"),
    ([{"key": "tpValue", "min": 3, "max": 1, "step": 0.5}], "min < max"),
    ([{"key": "@entry:0:0:left.period", "min": 2, "max": 200, "step": 1},
      {"key": "tpValue", "min": 0.5, "max": 3, "step": 0.5}], "조합"),
    ([{"key": "tpValue", "min": 1, "max": 2, "step": 0.5}, {"key": "tpValue", "min": 1, "max": 2, "step": 0.5}], "두 번"),
])
def test_check_rejects(sweep, msg):
    with pytest.raises(ValueError, match=msg):
        ai._check(sweep, C)


def test_propose_retries_once_and_reports_prior(env):
    ai.add_tested("내 프리셋", 40)
    env["fake"]([{"reply": "a", "sweep": [{"key": "leverage", "min": 1, "max": 5, "step": 1}]},
                 {"reply": "RSI 기간과 익절", "sweep": [{"key": "@entry:0:0:left.period", "min": 7, "max": 21, "step": 7},
                                                   {"key": "tpValue", "min": 1, "max": 3, "step": 0.5}]}])
    r = ai.propose({"presetName": "내 프리셋", "candidates": CANDS, "params": FORM})
    assert r["combos"] == 3 * 5 and r["testedBefore"] == 40
    assert "40개" in env["calls"][0] and "안 된다" in env["calls"][1]


def test_ledger_per_preset(env):
    assert ai.tested("A") == 0
    ai.add_tested("A", 10)
    ai.add_tested("A", 5)
    ai.add_tested("", 3)
    assert ai.tested("A") == 15 and ai.tested("B") == 0 and ai.tested(None) == 3


def _wavy(n=30 * 1440):
    rng = np.random.default_rng(5)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    return Candles(open_time=(np.arange(n) * 60_000).astype(np.int64), open=close, high=close * 1.001,
                   low=close * 0.999, close=close, volume=np.full(n, 10.0), timeframe_min=1)


def test_ai_optimize_run_accumulates_gate(env, monkeypatch):
    monkeypatch.setattr(server, "_get_candles", lambda sym, days: (_wavy(), 0.0001))
    p = dict(FORM, presetName="P", aiOpt=True, useOOS=True, minTrades=5,
             sweep={"@entry:0:0:left.period": {"min": 7, "max": 14, "step": 7}})
    outs = []
    server._run_optimize(p, outs.append)
    assert outs[0]["gateCombos"] == 2 and outs[-1]["gateCombos"] == 2
    outs.clear()
    server._run_optimize(p, outs.append)
    done = outs[-1]
    assert done["gateCombos"] == 4                               # 두 번째는 누적 4개 기준
    top = [t for t in done["top"] if t.get("null") and "combos" in t["null"]]
    assert top and all(t["null"]["combos"] == 4 for t in top)    # 귀무 게이트가 누적 N 으로 돌았다
    # 일반 범위 테스트(aiOpt 없음)는 누적에 안 들어간다
    server._run_optimize(dict(p, aiOpt=False), outs.append)
    assert ai.tested("P") == 4
    vs = ai.verdicts("P")                                        # AI 최적화 두 번만 판정 기록(일반 범위 테스트는 아님)
    assert [v["gateN"] for v in vs] == [2, 4] and all("pass" in v and v["top"] for v in vs)


def test_explain_passes_gate_n(env):
    env["fake"]([{"reply": "통과 없음"}])
    r = ai.explain({"presetName": "P", "labels": {"tpValue": "익절 값"}, "reason": "x",
                    "result": {"totalCombos": 5, "passed": 5, "gateCombos": 25,
                               "top": [{"params": {"tpValue": 2.0}, "is": {"return": 3}, "oos": {"return": -1},
                                        "null": {"beatsBestOfN": False, "negative": True}}]}})
    assert r["reply"] == "통과 없음"
    assert "N(누적 시험 조합) 25개" in env["calls"][0] and "익절 값" in env["calls"][0]


def test_time_model_matches_measurements():
    """실측(8코어, BTC 365일, 랭킹 재실행 포함): (백테스트 1회 초, 조합 수, 실제 초) — 영상 프리셋 6개 + 1m 60조합."""
    runs = [(1.02, 396, 58.2), (1.16, 315, 51.2), (1.32, 455, 78.1), (3.0, 180, 100.4), (2.35, 175, 74.8),
            (1.04, 468, 70.3), (2.5, 60, 52.7)]
    for one, n, sec in runs:
        tm = ai.time_model(one, 365, workers=8)
        assert tm["overheadSec"] + n * tm["secPerCombo"] == pytest.approx(sec, rel=0.15)
    assert ai.time_model(2.0, 365, 8)["overheadSec"] > ai.time_model(2.0, 300, 8)["overheadSec"]   # 1년 이상은 랭킹 재실행


def test_propose_budget_sets_combo_cap(env, monkeypatch):
    monkeypatch.setattr(ai, "_time_backtest", lambda form: (1.0, 300.0))
    from engine import optimize as opt
    monkeypatch.setattr(opt, "resolve_workers", lambda n, w=None: 8)          # CI 코어 수와 무관하게
    tm = ai.time_model(1.0, 300, 8)
    cap = ai.max_combos_for(60, tm)
    assert cap == int((60 - tm["overheadSec"]) / tm["secPerCombo"]) and ai.max_combos_for(600, tm) > 5 * cap
    big = {"key": "@entry:0:0:left.period", "min": 2, "max": 200, "step": 1}                       # 199조합
    env["fake"]([{"reply": "x", "sweep": [big, {"key": "tpValue", "min": 0.5, "max": 3, "step": 0.5}]},     # 1194 → 1분엔 초과
                 {"reply": "y", "sweep": [big]}])
    r = ai.propose({"presetName": "P", "candidates": CANDS, "params": FORM, "budgetSec": 60})
    assert r["combos"] == 199 and r["maxCombos"] == cap and r["budgetSec"] == 60 and r["secPerCombo"] > 0
    assert f"{cap}개 이하" in env["calls"][0] and "1분 이내" in env["calls"][0]
    with pytest.raises(ValueError):
        ai.propose({"presetName": "P", "candidates": CANDS, "params": FORM, "budgetSec": 77})
