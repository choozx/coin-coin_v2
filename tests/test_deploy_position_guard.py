"""배포 가드 — '모르면 연기'가 지켜지는가.

2026-09-02: 50번 넘게 정상 연기하던 가드가 딱 한 번 통과해, 포지션을 들고 있던 트레이더가
교체되고 손절/익절이 유실됐다. 그 한 번이 어떤 경로였는지는 로그가 지워져 못 밝혔다.
그래서 경로를 쫓는 대신 **판정 규칙 자체**를 안전측으로 바꾸고 여기서 잠근다.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy"))

from position_guard import DEAD_MS, STALE_MS, decide      # noqa: E402

NOW = 1_788_000_000_000


def _state(position=None, age_ms=0):
    return {"updatedAt": NOW - age_ms, "position": position}


POS = {"side": -1, "qty": 0.064}


def test_holding_position_defers():
    assert decide(_state(POS), NOW)[0] is True


def test_flat_and_fresh_proceeds():
    assert decide(_state(None), NOW)[0] is False


def test_unreadable_state_defers():
    """★ 예전엔 '읽기 실패 = 포지션 없음' 이었다. 모르는 채로 교체하는 게 사고의 형태다."""
    assert decide(None, NOW)[0] is True


def test_broken_updated_at_defers():
    assert decide({"updatedAt": "??", "position": None}, NOW)[0] is True


def test_stale_state_defers_even_if_flat():
    """★ 낡은 상태의 position=null 은 '없다'가 아니라 '그때는 없었다' 다."""
    assert decide(_state(None, age_ms=STALE_MS + 1), NOW)[0] is True


def test_fresh_flat_still_proceeds():
    """정상 배포가 막히면 안 된다 — 신선한 무포지션은 그대로 통과."""
    assert decide(_state(None, age_ms=STALE_MS - 1), NOW)[0] is False


def test_dead_bot_does_not_block_forever():
    """★ 봇이 죽어 상태가 멈추면 통과시킨다 — 안 그러면 죽은 봇이 제 고침을 영영 막는다."""
    defer, why = decide(_state(POS, age_ms=DEAD_MS + 1), NOW)
    assert defer is False and "죽은" in why


def test_dead_threshold_is_well_above_stale():
    """두 임계값이 뒤집히면 '모르면 연기'가 통째로 무의미해진다."""
    assert DEAD_MS > STALE_MS * 2
