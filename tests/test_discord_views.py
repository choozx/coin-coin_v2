"""디스코드 조회 명령 — 표현 레이어(discord_views)와 봇의 순수 헬퍼 검증(discord.py 불필요).

봇 토큰·네트워크 없이 '무엇을 어떻게 보여주는가'를 고정한다. discord_bot 은 top-level 에서
discord 를 import 하지 않으므로(=run() 안에서만) 여기서 import 해도 안전하다.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import discord_views as v          # noqa: E402
from engine import discord_bot as bot          # noqa: E402


def _state(**over):
    s = {
        "mode": "testnet", "paused": False, "preset": "슈퍼트렌드_V1",
        "symbol": "BTCUSDC", "timeframe": "15m", "equity": 4971.0,
        "returnPct": -0.58, "numTrades": 3, "updatedAt": 1_700_000_000_000,
        "position": {
            "side": 1, "entryPrice": 65095.0, "qty": 0.01, "leverage": 10,
            "stop": 64000.0, "tp": 66000.0, "liq": 60000.0,
            "entryTime": 1_699_980_000_000, "mark": 65200.0, "uPnl": 1.05, "uPnlPct": 1.6},
    }
    s.update(over)
    return s


# ---- /status ----

def test_status_shows_mode_run_and_position():
    t = v.status_text(_state())
    assert "테스트넷" in t and "실행중" in t
    assert "65,095" in t                        # 진입가 천단위 포맷
    assert "롱" in t and "+1.05" in t           # 포지션 요약 + 미실현손익 부호
    assert "무포지션" not in t


def test_status_flat_and_paused():
    t = v.status_text(_state(position=None, paused=True))
    assert "무포지션" in t and "멈춤" in t


def test_status_surfaces_guardrail_and_pending():
    t = v.status_text(_state(guardrail="일일 손실 한도", pendingStrategy="presets/x.json"))
    assert "가드레일" in t and "전환 대기" in t


def test_status_error_state():
    assert "⚠️" in v.status_text({"error": "상태 없음"})
    assert "⚠️" in v.status_text({})


# ---- /position ----

def test_position_detail_has_mark_and_upnl():
    t = v.position_text(_state())
    assert "롱 LONG" in t and "현재가" in t and "미실현손익" in t
    assert "65,200" in t and "+1.05" in t and "+1.60%" in t
    assert "보유" in t                          # 보유 시간


def test_position_flat():
    assert "무포지션" in v.position_text(_state(position=None))


# ---- /stats ----

def test_stats_empty():
    assert "없습니다" in v.stats_text({"n": 0}, "오늘")
    assert "없습니다" in v.stats_text({}, "전체")


def test_stats_overall():
    s = {"n": 10, "wins": 6, "winRate": 60.0, "totalPnl": 123.45, "avgPnl": 12.35,
         "profitFactor": 1.8, "maxDrawdown": 40.0, "byStrategy": []}
    t = v.stats_text(s, "7일")
    assert "[7일]" in t and "60.0%" in t and "+123.45" in t
    assert "1.8" in t and "40.00" in t
    assert "전략별" not in t                     # 전략 1개 이하면 분해 안 함


def test_stats_breaks_down_by_strategy_when_multiple():
    s = {"n": 5, "wins": 3, "winRate": 60.0, "totalPnl": 50.0, "avgPnl": 10.0,
         "profitFactor": 2.0, "maxDrawdown": 10.0,
         "byStrategy": [
             {"strategy": "A", "n": 3, "winRate": 66.7, "totalPnl": 40.0},
             {"strategy": "B", "n": 2, "winRate": 50.0, "totalPnl": 10.0}]}
    t = v.stats_text(s, "전체")
    assert "전략별" in t and "`A`" in t and "`B`" in t


# ---- /control 패널 ----

def test_control_text_running_and_paused():
    r = v.control_text(paused=False, has_position=False)
    assert "실행중" in r and "시작" in r and "정지" in r
    p = v.control_text(paused=True, has_position=False)
    assert "정지됨" in p


def test_control_text_warns_when_holding_position():
    """실행중 + 포지션 보유면 '정지해도 자연청산까지 관리' 안내가 붙는다(graceful 강조)."""
    t = v.control_text(paused=False, has_position=True)
    assert "자연 청산까지 관리" in t
    # 이미 정지 상태면 그 안내는 불필요
    assert "자연 청산까지 관리" not in v.control_text(paused=True, has_position=True)


# ---- period_bounds ----

def test_period_bounds():
    now = 1_700_000_000_000
    today, l1 = v.period_bounds("today", now)
    assert l1 == "오늘" and today <= now and now - today < 86_400_000
    assert v.period_bounds("7d", now) == (now - 7 * 86_400_000, "7일")
    assert v.period_bounds("30d", now) == (now - 30 * 86_400_000, "30일")
    assert v.period_bounds("all", now) == (None, "전체")
    assert v.period_bounds(None, now) == (None, "전체")   # 기본값 = 전체


# ---- 봇 순수 헬퍼 ----

def test_allowed_ids_parsing():
    assert bot.allowed_ids("123, 456") == {123, 456}
    assert bot.allowed_ids("") == set()
    assert bot.allowed_ids("abc,789") == {789}           # 숫자 아닌 건 버림


def test_load_state_missing_file_returns_error(tmp_path=None):
    import tempfile
    missing = os.path.join(tempfile.mkdtemp(), "nope.json")
    assert "error" in bot.load_state(missing)


def test_load_state_reads_json():
    import json
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "state.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"mode": "paper", "equity": 100}, f)
    assert bot.load_state(p)["equity"] == 100


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
