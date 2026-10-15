"""백테스트 결과 랭킹 저장소 — 등록·덮어쓰기·설정 변형·삭제·요약. 네트워크 없음."""
from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import ranking   # noqa: E402

PRESET = {"schemaVersion": "1.0", "name": "x", "market": {"symbol": "BTCUSDT", "timeframe": "5m", "direction": "long"},
          "entryRules": [{"side": "long", "when": {"op": "AND", "children": [
              {"left": {"indicator": "RSI", "period": 14}, "cmp": "<", "right": 30},
              {"cross": "crossOver", "left": {"indicator": "EMA", "period": 20}, "right": {"indicator": "EMA", "period": 50}}]}}],
          "exit": {"stopLoss": {"type": "indicator", "indicator": {"indicator": "EMA", "period": 50}, "bufferPercent": 0.05},
                   "takeProfit": {"type": "riskReward", "value": 1.5}, "timeStop": {"maxBars": 48}},
          "sizing": {"leverage": 10, "marginMode": "isolated", "size": {"type": "equityPercent", "value": 10}}}


def _result(preset, ret=12.0, mdd=6.0, days=365, end=1_788_134_400_000):
    return {"preset": preset, "dataRange": [end - int(days * 86_400_000), end], "buyHoldPct": 3.0,
            "verdict": {"beatsNull": True, "negative": False, "percentile": 97.0},
            "metrics": {"totalReturnPct": ret, "initialEquity": 10000, "numTrades": 40, "winRatePct": 45.0,
                        "profitFactor": 1.3, "maxDrawdownPct": mdd, "sharpe": 1.1, "numLiquidations": 0, "totalFees": 12.0}}


def test_record_dedupes_same_settings_and_separates_variants(tmp_path, monkeypatch):
    monkeypatch.setattr(ranking, "PATH", str(tmp_path / "r.jsonl"))
    k1 = ranking.record({"presetName": "A", "form": {"f": 1}}, _result(PRESET, ret=10))
    k2 = ranking.record({"presetName": "A"}, _result(PRESET, ret=20, end=1_788_220_800_000))   # 하루 뒤 같은 길이 재실행 → 덮어씀
    assert k1 == k2
    rows = ranking.list_results()
    assert len(rows) == 1 and rows[0]["returnPct"] == 20 and "form" not in rows[0]
    v = copy.deepcopy(PRESET)
    v["exit"]["takeProfit"]["value"] = 2.0                                             # 설정 하나만 달라도 다른 줄
    ranking.record({"presetName": "A"}, _result(v))
    ranking.record({"presetName": "A"}, _result(PRESET, days=730))                       # 기간이 다르면 다른 줄
    assert len(ranking.list_results()) == 3
    assert ranking.record({"presetName": "A"}, _result(PRESET, days=200)) is None         # 1년 미만은 등록 안 함
    assert ranking.record({"presetName": "A"}, _result(PRESET, days=364.9993)) is not None  # '365일' 입력(1분 모자람)은 등록
    v2 = copy.deepcopy(PRESET)
    v2["name"], v2["description"] = "다른 이름", "설명만 다름"                              # 이름·설명은 설정이 아니다
    assert ranking.settings_key(v2) == ranking.settings_key(PRESET)


def test_metrics_and_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(ranking, "PATH", str(tmp_path / "r.jsonl"))
    k = ranking.record({"presetName": ""}, _result(PRESET, ret=21.0, mdd=7.0, days=365.25))
    r = ranking.get({"key": k})
    assert r["presetName"] == "(이름 없음)" and r["beatsRandom"] is True
    assert abs(r["cagrPct"] - 21.0) < 0.01 and abs(r["calmar"] - 3.0) < 0.01           # 1년이면 연환산 = 총수익
    ranking.delete({"key": k})
    assert ranking.list_results() == []


def test_describe_reads_like_settings():
    d = ranking.describe(PRESET)
    assert d["entry"] == "롱: RSI(14) < 30 · EMA(20) ▲ EMA(50)"
    assert d["exit"] == "손절 진입 때 EMA(50)±0.05% · 익절 손절폭×1.5 · 48봉 시간청산"
    assert d["sizing"] == "자본 10% · 10배"


def test_range_source_is_kept_and_separate_from_plain_backtest(tmp_path, monkeypatch):
    monkeypatch.setattr(ranking, "PATH", str(tmp_path / "r.jsonl"))
    v = copy.deepcopy(PRESET)
    v["exit"]["takeProfit"]["value"] = 3.0
    k = ranking.record({"presetName": "A", "form": {"f": 1}}, _result(v),
                       source={"kind": "range", "rank": 2, "combos": 120, "params": {"tpValue": 3.0}})
    r = ranking.get({"key": k})
    assert r["source"] == {"kind": "range", "rank": 2, "combos": 120, "params": {"tpValue": 3.0}}
    ranking.record({"presetName": "A"}, _result(PRESET))
    assert sorted(x["source"] is None for x in ranking.list_results()) == [False, True]


def test_range_rerun_does_not_relabel_a_plain_backtest(tmp_path, monkeypatch):
    monkeypatch.setattr(ranking, "PATH", str(tmp_path / "r.jsonl"))
    k1 = ranking.record({"presetName": "A"}, _result(PRESET, ret=10))
    k2 = ranking.record({"presetName": "A"}, _result(PRESET, ret=10), source={"kind": "range", "rank": 1, "combos": 3, "params": {}})
    assert k1 == k2 and ranking.get({"key": k1})["source"] is None
