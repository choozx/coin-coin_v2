"""연속 손실 가드레일 — 자기 해제가 되는가(래치 방지). 네트워크·거래소 없음.

배경: 이 가드레일은 원래 **스스로 풀리지 않는 래치**였다. N연패로 발동하면 새 진입이 막히고
→ 새 트레이드가 안 생기고 → 연속 기록이 영원히 그대로라, 사람이 설정을 끄기 전까지 봇이
영구 정지했다. 쿨다운(시간)과 기준선(여기서부터 다시 센다)으로 그 고리를 끊는다.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.live import loss_streak_block      # noqa: E402


NOW = 1_700_000_000_000
HOUR = 3_600_000


class T:
    """ClosedTrade 중 이 판정이 쓰는 두 필드만."""
    def __init__(self, pnl, exit_time):
        self.pnl, self.exit_time = pnl, exit_time


def _losses(n, last_ms=NOW):
    """가장 최근이 last_ms 인 n연패(1시간 간격)."""
    return [T(-10.0, last_ms - (n - 1 - i) * HOUR) for i in range(n)]


def test_blocks_at_threshold():
    reason, reset = loss_streak_block(_losses(5), 5, 12, 0, NOW)
    assert reason and "연속 손실 5회" in reason and reset is None


def test_below_threshold_passes():
    assert loss_streak_block(_losses(4), 5, 12, 0, NOW) == (None, None)


def test_win_breaks_the_streak():
    """중간에 이긴 트레이드가 있으면 거기서 끊긴다 — '연속'의 정의."""
    trades = _losses(3) + [T(+20.0, NOW + HOUR)] + _losses(2, NOW + 3 * HOUR)
    assert loss_streak_block(trades, 5, 12, 0, NOW + 4 * HOUR) == (None, None)


def test_cooldown_releases_and_moves_baseline():
    """★ 래치 해소: 쿨다운이 지나면 차단이 풀리고 기준선을 지금으로 올린다."""
    trades = _losses(5)
    blocked, _ = loss_streak_block(trades, 5, 12, 0, NOW + 6 * HOUR)
    assert blocked                                        # 6시간 뒤: 아직 차단
    reason, reset = loss_streak_block(trades, 5, 12, 0, NOW + 13 * HOUR)
    assert reason is None and reset == NOW + 13 * HOUR    # 12시간 뒤: 해제 + 기준선 갱신


def test_baseline_makes_old_losses_invisible():
    """기준선을 올린 뒤엔 그 이전 연패가 다시 발동시키지 않는다(회로 재폐쇄)."""
    trades = _losses(5)
    assert loss_streak_block(trades, 5, 12, NOW + HOUR, NOW + 2 * HOUR) == (None, None)


def test_needs_full_count_again_after_reset():
    """리셋 뒤엔 다시 count 번을 져야 발동한다 — 한 번 졌다고 즉시 재발동하지 않는다."""
    reset_at = NOW
    after = _losses(4, NOW + 5 * HOUR)                    # 리셋 후 4연패(임계 5)
    assert loss_streak_block(after, 5, 12, reset_at, NOW + 6 * HOUR) == (None, None)
    after5 = _losses(5, NOW + 6 * HOUR)
    reason, _ = loss_streak_block(after5, 5, 12, reset_at, NOW + 6 * HOUR)
    assert reason


def test_zero_cooldown_keeps_latch_but_says_so():
    """쿨다운 0 은 옛 동작(수동 해제 전용) — 다만 갇힌 줄 모르지 않게 사유에 적는다."""
    reason, reset = loss_streak_block(_losses(5), 5, 0, 0, NOW + 999 * HOUR)
    assert reason and "자동 해제 없음" in reason and reset is None


def test_reason_is_stable_while_blocked():
    """★ 사유 문자열이 폴링마다 바뀌면 안 된다 — 바뀌면 60초마다 알림이 나간다.

    그래서 '남은 시간'이 아니라 '해제 시각'으로 적는다.
    """
    trades = _losses(5)
    a, _ = loss_streak_block(trades, 5, 12, 0, NOW + 1 * HOUR)
    b, _ = loss_streak_block(trades, 5, 12, 0, NOW + 2 * HOUR)
    c, _ = loss_streak_block(trades, 5, 12, 0, NOW + 11 * HOUR)
    assert a == b == c


def test_empty_ledger_is_safe():
    assert loss_streak_block([], 5, 12, 0, NOW) == (None, None)
    assert loss_streak_block(None, 5, 12, 0, NOW) == (None, None)
