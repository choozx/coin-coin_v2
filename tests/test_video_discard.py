"""영상 프리셋 폐기 ↔ 되살리기 — 파일을 지우지 않고 보관함으로, 라이브 봇 전략은 막는다. 임시 폴더에서만."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import server, video_analyze as va       # noqa: E402

VID = "abcdefghijk"


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path
    saved, disc, cache = root / "presets" / "saved", root / "presets" / "discarded", root / "data" / "video"
    for d in (saved, cache / VID):
        d.mkdir(parents=True)
    monkeypatch.setattr(va, "ROOT", str(root))
    monkeypatch.setattr(va, "CACHE_DIR", str(cache))
    monkeypatch.setattr(va, "DISCARD_DIR", str(disc))
    monkeypatch.setattr(server, "_SAVED_DIR", str(saved))
    for n in ("🎬 A", "🎬 B"):
        (saved / f"{n}.json").write_text(json.dumps({"name": n, "form": {"x": 1}}), encoding="utf-8")
    (saved / "남의 것.json").write_text(json.dumps({"name": "남의 것", "form": {}}), encoding="utf-8")
    va.add_preset_record(VID, 0, "🎬 A")
    va.add_preset_record(VID, "chat", "🎬 B")
    return root, saved, disc


def test_discard_one_moves_to_archive_and_restore(env):
    root, saved, disc = env
    r = va.discard({"id": VID, "names": ["🎬 A"], "reason": "기각"})
    assert r["moved"] == ["🎬 A"] and not (saved / "🎬 A.json").exists()
    rec = json.loads((disc / "🎬 A.json").read_text(encoding="utf-8"))
    assert rec["form"] == {"x": 1} and rec["discarded"]["reason"] == "기각" and rec["discarded"]["videoId"] == VID
    assert r["presets"][0] == [{"name": "🎬 A", "exists": False, "discarded": True}]
    assert r["discarded"] is None                                   # 프리셋 하나만 — 영상은 그대로
    back = va.restore({"id": VID, "names": ["🎬 A"]})
    assert back["restored"] == ["🎬 A"] and "discarded" not in json.loads((saved / "🎬 A.json").read_text(encoding="utf-8"))
    assert not (disc / "🎬 A.json").exists()


def test_discard_video_moves_all_its_presets_only(env):
    root, saved, disc = env
    r = va.discard({"id": VID, "reason": "전부 기각", "video": True})
    assert sorted(r["moved"]) == ["🎬 A", "🎬 B"] and (saved / "남의 것.json").exists()
    assert r["discarded"]["reason"] == "전부 기각" and sorted(r["discarded"]["presets"]) == ["🎬 A", "🎬 B"]
    # 남의 프리셋 이름을 넣어도 이 영상 것만 옮긴다
    va.discard({"id": VID, "names": ["남의 것"]})
    assert (saved / "남의 것.json").exists()
    va.restore({"id": VID})
    assert (saved / "🎬 A.json").exists() and (saved / "🎬 B.json").exists() and va.discard_state(VID) is None


def test_restore_never_overwrites(env):
    root, saved, disc = env
    va.discard({"id": VID, "names": ["🎬 A"]})
    (saved / "🎬 A.json").write_text(json.dumps({"name": "🎬 A", "form": {"new": 1}}), encoding="utf-8")
    r = va.restore({"id": VID, "names": ["🎬 A"]})
    assert r["skipped"] == ["🎬 A"] and json.loads((saved / "🎬 A.json").read_text(encoding="utf-8"))["form"] == {"new": 1}
    assert (disc / "🎬 A.json").exists()                            # 보관본도 그대로


def test_live_strategy_cannot_be_discarded(env):
    root, saved, disc = env
    (root / "data" / "control.json").write_text(json.dumps({"strategy": "presets/saved/🎬 B.json"}), encoding="utf-8")
    with pytest.raises(ValueError, match="봇"):
        va.discard({"id": VID, "video": True})
    assert (saved / "🎬 A.json").exists() and (saved / "🎬 B.json").exists()   # 하나도 안 옮김
    assert va.discard({"id": VID, "names": ["🎬 A"]})["moved"] == ["🎬 A"]


def test_list_puts_discarded_last(env):
    root, saved, disc = env
    other = "zyxwvutsrqp"
    for vid in (VID, other):
        d = root / "data" / "video" / vid
        d.mkdir(exist_ok=True)
        (d / "meta.json").write_text(json.dumps({"title": vid, "url": "u"}), encoding="utf-8")
        (d / "analysis.json").write_text(json.dumps({"_analyzedAt": 2 if vid == VID else 1}), encoding="utf-8")
    assert [x["id"] for x in va.list_analyses()] == [VID, other]
    va.discard({"id": VID, "video": True})
    rows = va.list_analyses()
    assert [x["id"] for x in rows] == [other, VID] and rows[1]["discarded"] and rows[1]["presetCount"] == 0
