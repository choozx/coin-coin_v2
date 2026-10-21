"""영상 분석 화면의 유튜브 검색 — 제외 채널(채널 ID 기준) 거르기, 분석함 표시, 채널·재생목록 결과 건너뛰기. yt-dlp 는 가짜."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import video_analyze as va             # noqa: E402

ENTRIES = [
    {"id": "AAAAAAAAAAA", "title": "RSI 매매법", "channel": "좋은채널", "channel_id": "UCgood000001", "duration": 600, "view_count": 1200},
    {"id": "BBBBBBBBBBB", "title": "100배 수익 인증", "channel": "광고채널", "channel_id": "UCspam000001", "duration": 60, "view_count": 9},
    {"id": "UCxxxxxxxxxxxxxxxxxxxxxx", "title": "채널 결과", "channel": "x", "channel_id": "UCx"},       # 영상 아님
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(va, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(va, "EXCLUDED_PATH", str(tmp_path / "excluded_channels.json"))
    monkeypatch.setattr(va.shutil, "which", lambda _: __file__)          # 아무 존재하는 경로
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(stdout=json.dumps({"entries": ENTRIES}), stderr="", returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    return {"calls": calls, "dir": tmp_path}


def test_search_filters_and_marks(env):
    (env["dir"] / "AAAAAAAAAAA").mkdir()
    (env["dir"] / "AAAAAAAAAAA" / "analysis.json").write_text("{}")
    r = va.search({"q": "비트코인"})
    assert [x["id"] for x in r["items"]] == ["AAAAAAAAAAA", "BBBBBBBBBBB"]      # 채널 결과는 빠짐
    assert r["items"][0]["analyzed"] and not r["items"][1]["analyzed"]
    assert env["calls"][0][-1] == f"ytsearch{va.SEARCH_N}:비트코인"
    va.search({"q": "x", "sort": "date"})
    assert env["calls"][1][-1].startswith("ytsearchdate")


def test_exclude_by_channel_id_persists(env):
    va.exclude_channel({"channelId": "UCspam000001", "channel": "광고채널"})
    r = va.search({"q": "비트코인"})
    assert [x["id"] for x in r["items"]] == ["AAAAAAAAAAA"] and r["hidden"] == 1
    assert va.excluded_list()["excluded"] == [{"channelId": "UCspam000001", "channel": "광고채널"}]
    va.unexclude_channel({"channelId": "UCspam000001"})
    assert va.search({"q": "비트코인"})["hidden"] == 0


def test_bad_inputs(env):
    with pytest.raises(ValueError):
        va.search({"q": "  "})
    with pytest.raises(ValueError):
        va.exclude_channel({"channelId": "../../etc"})
