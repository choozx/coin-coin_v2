"""tools/fill_audit — 실거래 원장 ↔ 백테스트 짝짓기.

실거래 진입 시각은 봉 시각이 아니라 체결 시각이라 몇 초 늦다. 정확일치로 짝지으면
모든 거래가 '실거래에만 / 백테스트에만'으로 갈려 대조가 아무 말도 못 한다.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.metrics import Trade                      # noqa: E402
from tools.fill_audit import _match, _ret_bp           # noqa: E402

M = 60_000


def _bt(t):
    return Trade(side=1, entry_time=t, entry_price=100.0, exit_time=t + 10 * M,
                 exit_price=101.0, qty=1.0, leverage=1, pnl=1.0, fees=0.0, funding=0.0,
                 exit_reason="take_profit")


def test_match_tolerates_fill_delay_and_reports_unmatched():
    bt = [_bt(0), _bt(30 * M), _bt(60 * M)]
    rows = [{"entry_time": 7_000}, {"entry_time": 60 * M + 4_000}, {"entry_time": 200 * M}]
    pairs, only_live, only_bt = _match(rows, bt, tol_ms=5 * M)
    assert [(r["entry_time"], t.entry_time) for r, t in pairs] == [(7_000, 0), (60 * M + 4_000, 60 * M)]
    assert [r["entry_time"] for r in only_live] == [200 * M]
    assert [t.entry_time for t in only_bt] == [30 * M]


def test_match_is_one_to_one():
    pairs, only_live, _ = _match([{"entry_time": 1_000}, {"entry_time": 2_000}], [_bt(0)], tol_ms=5 * M)
    assert len(pairs) == 1 and len(only_live) == 1


def test_ret_bp_is_notional_normalized():
    # 롱 100→101, 수수료 0.2, 펀딩 0 → (1 - 0.2)/100 = 80bp, 수량과 무관
    assert abs(_ret_bp(1, 100.0, 101.0, 0.2, 0.0, 1.0) - 80.0) < 1e-9
    assert abs(_ret_bp(1, 100.0, 101.0, 2.0, 0.0, 10.0) - 80.0) < 1e-9
    assert abs(_ret_bp(-1, 100.0, 101.0, 0.0, 0.0, 1.0) + 100.0) < 1e-9
