"""영상 분석 '영상과 대화하며 프리셋 만들기'(영상 하나에 대화 하나) — 기록 저장·초안 검증·재시도·저장 경로. Claude 호출은 가짜로 바꾼다(네트워크·CLI 없음).

핵심 약속: Claude 가 낸 초안은 엔진 검증을 통과해야만 반영된다. 실패하면 한 번 고쳐 달라고 다시 묻고,
그래도 안 되면 이전 초안을 그대로 둔다. 저장은 사용자가 버튼(chat_save)으로만 한다.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import server                          # noqa: E402
from engine import video_analyze as va             # noqa: E402

VID = "AAAAAAAAAAA"


def _draft(name="RSI 반등", period=14, si=0):
    rsi = {"kind": "indicator", "indicator": "RSI", "period": period}
    return {"name": name, "timeframe": "15m",
            "groups": [{"side": "long", "conds": [{"left": rsi, "op": "crossOver",
                                                     "right": {"kind": "number", "value": 30}, "note": "RSI 30 상향"}]}],
            "stop": {"type": "swing", "lookback": 20}, "take": {"type": "riskReward", "value": 2},
            "exit_long": [], "exit_short": [], "time_stop_bars": 0, "assumed": [], "excluded": [], "strategy_index": si}


BAD = dict(_draft(), groups=[])                    # 진입 조건 없음 → 검증 실패


@pytest.fixture
def env(tmp_path, monkeypatch):
    cache, saved = tmp_path / "video", tmp_path / "saved"
    (cache / VID).mkdir(parents=True)
    (cache / VID / "meta.json").write_text(json.dumps(
        {"id": VID, "title": "t", "channel": "c", "description": "", "transcript": "[00:00:01] RSI 30 돌파에 산다"}))
    (cache / VID / "analysis.json").write_text(json.dumps(
        {"indicators": [], "strategies": [{"name": "RSI", "direction": "long", "timeframe": "15m",
                                           "rules": [{"part": "진입", "text": "RSI 30 돌파", "status": "ready", "how": ""}]}]}))
    monkeypatch.setattr(va, "CACHE_DIR", str(cache))
    monkeypatch.setattr(server, "_SAVED_DIR", str(saved))
    calls = []

    def fake(replies):
        it = iter(replies)

        def _claude(prompt, schema, system, timeout=600):
            calls.append(prompt)
            return next(it), {"cost": 0.01, "seconds": 1.0, "model": "x"}
        monkeypatch.setattr(va, "_claude", _claude)
    return {"fake": fake, "calls": calls, "saved": saved}


def test_question_only_keeps_draft_and_history(env):
    env["fake"]([{"reply": "손절 얘기는 없어요"}])
    st = va.chat({"id": VID, "message": "손절은?"})
    assert st["draft"] is None and [m["role"] for m in st["messages"]] == ["user", "assistant"]
    assert not st["messages"][1]["draftChanged"]
    # 다시 열면 그대로
    assert va.chat_load({"id": VID})["messages"] == st["messages"]


def test_valid_draft_applied_and_history_resent(env):
    env["fake"]([{"reply": "만들었어요", "draft": _draft()}, {"reply": "21로 바꿨어요", "draft": _draft(period=21)}])
    va.chat({"id": VID, "message": "초안 만들어줘"})
    st = va.chat({"id": VID, "message": "RSI 21로"})
    assert st["draft"]["groups"][0]["conds"][0]["left"]["period"] == 21
    second = env["calls"][1]
    assert "초안 만들어줘" in second and '"period": 14' in second      # 지난 대화·현재 초안을 다시 보낸다
    assert st["costUsd"] == pytest.approx(0.02)


def test_invalid_draft_retried_once_then_fixed(env):
    env["fake"]([{"reply": "a", "draft": BAD}, {"reply": "고쳤어요", "draft": _draft()}])
    st = va.chat({"id": VID, "message": "만들어줘"})
    assert len(env["calls"]) == 2 and "검증에 실패" in env["calls"][1]
    assert st["draft"]["name"] == "RSI 반등" and st["draftError"] is None
    assert st["messages"][1]["text"] == "고쳤어요"


def test_invalid_draft_twice_keeps_previous(env):
    env["fake"]([{"reply": "ok", "draft": _draft()}, {"reply": "b", "draft": BAD}, {"reply": "c", "draft": BAD}])
    va.chat({"id": VID, "message": "만들어줘"})
    st = va.chat({"id": VID, "message": "진입 빼줘"})
    assert st["draft"]["name"] == "RSI 반등"                      # 이전 초안 유지
    assert st["draftError"] and st["messages"][-1]["draftError"]
    assert not st["messages"][-1]["draftChanged"]


def test_save_writes_preset_and_record(env):
    env["fake"]([{"reply": "ok", "draft": _draft()}])
    va.chat({"id": VID, "message": "만들어줘"})
    r = va.chat_save({"id": VID, "name": "내 RSI"})
    assert r["name"] == "🎬 내 RSI" and r["built"]
    rec = json.loads((env["saved"] / (server._safe_name(r["name"]) + ".json")).read_text())
    assert rec["preset"]                                         # 스키마형 프리셋까지 빌드됨
    assert r["chat"]["presets"] == {0: [{"name": "🎬 내 RSI", "exists": True}]}   # 기반 매매법 카드에


def test_save_without_draft_fails(env):
    with pytest.raises(ValueError):
        va.chat_save({"id": VID})


def test_save_without_strategy_goes_to_chat_record(env):
    env["fake"]([{"reply": "섞었어요", "draft": _draft(name="섞음", si=-1)}])
    va.chat({"id": VID, "message": "둘 섞어줘"})
    r = va.chat_save({"id": VID})
    assert r["name"] == "🎬 섞음"
    assert r["chat"]["presets"] == {"chat": [{"name": "🎬 섞음", "exists": True}]}
    assert va.list_analyses()[0]["presetCount"] == 1                # 왼쪽 목록 개수에도 잡힌다


def test_whole_video_in_prompt_and_reset(env):
    env["fake"]([{"reply": "ok"}])
    va.chat({"id": VID, "message": "?"})
    assert '"index": 0' in env["calls"][0] and "RSI 30 돌파" in env["calls"][0]
    assert "AI 최적화" in env["calls"][0]                         # 최적화는 백테스트 화면으로 안내
    st = va.chat_reset({"id": VID})
    assert st["messages"] == [] and st["draft"] is None


def test_bad_ids_rejected(env):
    with pytest.raises(ValueError):
        va.chat_load({"id": "../../etc"})
    with pytest.raises(ValueError):
        va.chat({"id": VID, "message": "   "})

