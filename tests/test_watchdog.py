"""워치독(데드맨 스위치) — 상태 판정·전이 알림 로직 검증(네트워크 없음).

핵심: 트레이더가 죽어 state.json 갱신이 끊기면 '멈춤'을 판정하고, 처음부터 죽어 있어도
(워치독 켜질 때 이미 stale) 놓치지 않고 알린다. 알림은 전이 시에만(스팸 방지).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import watchdog as w              # noqa: E402


NOW = 1_700_000_000_000


def test_evaluate_ok_vs_stale():
    # 5분 전 갱신, 임계 10분 → ok
    ok, age = w.evaluate(NOW - 5 * 60_000, NOW, stale_sec=600)
    assert ok == "ok" and 299 < age < 301
    # 20분 전 갱신 → stale
    stale, age2 = w.evaluate(NOW - 20 * 60_000, NOW, stale_sec=600)
    assert stale == "stale" and age2 > 600


def test_evaluate_missing():
    status, age = w.evaluate(None, NOW, stale_sec=600)
    assert status == "missing" and age is None


def test_alert_only_on_transition():
    # 같은 상태 반복 → 알림 없음(스팸 방지)
    assert w.alert_for("ok", "ok", 10) is None
    assert w.alert_for("stale", "stale", 999) is None


def test_alert_fires_when_starts_already_dead():
    """워치독이 켜질 때 트레이더가 이미 죽어 있으면(prev=None→stale) 바로 알린다 — 지금 상황."""
    msg = w.alert_for(None, "stale", 3 * 3600)     # 3시간 무갱신
    assert msg and "응답 없음" in msg and "180분" in msg
    assert w.alert_for(None, "missing", None) and "없음" in w.alert_for(None, "missing", None)


def test_alert_recovery():
    assert "복구" in w.alert_for("stale", "ok", 1)
    assert "복구" in w.alert_for("missing", "ok", 1)
    # 정상 기동(None→ok)은 조용히
    assert w.alert_for(None, "ok", 1) is None


def test_startup_text_reports_current_state():
    assert "정상" in w.startup_text("ok", 120, 600) and "2분 전" in w.startup_text("ok", 120, 600)
    assert "응답 없음" in w.startup_text("stale", 3600, 600) and "60분째" in w.startup_text("stale", 3600, 600)
    assert "상태 파일 없음" in w.startup_text("missing", None, 600)
    assert "10분 이상" in w.startup_text("ok", 0, 600)      # stale_sec 600 → 10분


def test_collect_status_silent_when_collector_paused():
    """수집기를 일부러 멈춰뒀으면 'paused' — 내가 멈춘 걸 경고받을 이유가 없다."""
    d = tempfile.mkdtemp()
    ctrl = os.path.join(d, "control.json")
    with open(ctrl, "w", encoding="utf-8") as f:
        json.dump({"collector": "paused"}, f)
    assert w.collect_status("/nope/none.db", 10, ctrl) == ("paused", None)
    # 멈춤이 아니면 실제 판정으로 넘어간다(캐시 없음 → empty)
    with open(ctrl, "w", encoding="utf-8") as f:
        json.dump({"collector": "running"}, f)
    assert w.collect_status("/nope/none.db", 10, ctrl)[0] == "empty"


def test_read_updated_ms():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "state.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"updatedAt": 123, "equity": 1}, f)
    assert w.read_updated_ms(p) == 123
    assert w.read_updated_ms(os.path.join(d, "nope.json")) is None   # 파일 없음
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ broken")
    assert w.read_updated_ms(p) == 0                                 # 못 읽으면 오래됨 취급


if __name__ == "__main__":
    import traceback
    fns = [x for k, x in sorted(globals().items()) if k.startswith("test_") and callable(x)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
