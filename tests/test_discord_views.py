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


# ---- /info · effective_config ----

def _info(config=None, defaults=None):
    return {"config": config or {}, "presetDefaults": defaults or {}}


def test_effective_config_prefers_override_else_preset():
    dflt = {"symbol": "BTCUSDC", "sizing": {"marginMode": "isolated", "leverage": 20,
            "size": {"type": "equityPercent", "value": 10}},
            "execution": {"entryType": "makerLimit", "makerTimeoutSeconds": 2}}
    # 오버라이드 없음 → 프리셋 값 그대로
    e = v.effective_config(_info(defaults=dflt))
    assert e["leverage"] == 20 and e["equityPercent"] == 10 and e["symbol"] == "BTCUSDC"
    assert e["makerTimeoutSeconds"] == 2 and e["marginMode"] == "isolated"
    # 봇 설정이 레버리지·심볼을 덮으면 그게 이긴다
    e2 = v.effective_config(_info(config={"symbol": "ETHUSDC", "sizing": {"leverage": 5},
                                          "useDynamicLeverage": True}, defaults=dflt))
    assert e2["leverage"] == 5 and e2["symbol"] == "ETHUSDC" and e2["useDynamicLeverage"] is True


def test_info_text_shows_leverage_mode():
    dflt = {"symbol": "BTCUSDC", "sizing": {"leverage": 20, "marginMode": "isolated",
            "size": {"type": "equityPercent", "value": 10}},
            "execution": {"entryType": "makerLimit", "makerTimeoutSeconds": 3}}
    t = v.info_text({"mode": "testnet", "preset": "슈퍼트렌드_V1", "timeframe": "15m"}, _info(defaults=dflt))
    assert "봇 정보" in t and "20x 고정" in t and "BTCUSDC" in t and "maker 지정가" in t
    dyn = v.info_text({"mode": "live"}, _info(config={"useDynamicLeverage": True}, defaults=dflt))
    assert "동적 티어" in dyn


# ---- /config · parse + apply ----

def test_parse_config_form_validates_and_skips_blanks():
    edits, errs = v.parse_config_form({"leverage": "10", "equity_percent": "25",
                                       "maker_timeout": "", "symbol": " btcusdc "})
    assert edits == {"leverage": 10, "equityPercent": 25.0, "symbol": "BTCUSDC"}
    assert errs == []


def test_parse_config_form_rejects_bad_ranges():
    _, e1 = v.parse_config_form({"leverage": "0"})
    _, e2 = v.parse_config_form({"leverage": "200"})
    _, e3 = v.parse_config_form({"equity_percent": "150"})
    _, e4 = v.parse_config_form({"leverage": "abc"})
    _, e5 = v.parse_config_form({"maker_timeout": "-1"})
    assert e1 and e2 and e3 and e4 and e5


def test_apply_config_edits_preserves_other_keys():
    """set_bot_config 는 통째 교체 → 병합이 useDynamicLeverage·filter·다른 sizing 키를 지켜야 한다."""
    current = {"useDynamicLeverage": True, "filter": {"minAdx": 20},
               "sizing": {"marginMode": "isolated", "leverage": 20}}
    out = v.apply_config_edits(current, {"leverage": 5, "equityPercent": 30, "symbol": "ETHUSDC"})
    assert out["useDynamicLeverage"] is True              # 안 날아감
    assert out["filter"] == {"minAdx": 20}
    assert out["sizing"]["marginMode"] == "isolated"      # 기존 sizing 키 유지
    assert out["sizing"]["leverage"] == 5
    assert out["sizing"]["size"] == {"type": "equityPercent", "value": 30}
    assert out["symbol"] == "ETHUSDC"
    # 원본 불변(deepcopy)
    assert current["sizing"]["leverage"] == 20


def test_config_and_strategy_confirm_text_note_position():
    c = v.config_confirm_text({"leverage": 10}, has_position=True)
    assert "변경 확인" in c and "레버리지" in c and "청산 후" in c
    s = v.strategy_confirm_text({"name": "X", "symbol": "BTCUSDC", "timeframe": "15m"}, has_position=False)
    assert "전략 전환 확인" in s and "즉시 적용" in s


# ---- 정기 요약 (daily_digest) ----

def _row(side=1, entry=100.0, exit=101.0, pnl=1.0, reason="signal"):
    return {"side": side, "entry_price": entry, "exit_price": exit, "pnl": pnl, "reason": reason}


def test_daily_digest_empty_shows_balance_only():
    t = v.daily_digest_text([], {"n": 0}, {"equity": 4971.0, "position": None})
    assert "지난 24시간" in t and "거래 없음" in t and "4,971" in t and "무포지션" in t


def test_daily_digest_summarizes_trades():
    rows = [_row(1, 65000, 65300, 30.0, "take_profit"),
            _row(-1, 64000, 64200, -20.0, "supertrend")]
    stats = {"n": 2, "wins": 1, "winRate": 50.0, "totalPnl": 10.0, "avgPnl": 5.0,
             "profitFactor": 1.5, "maxDrawdown": 20.0}
    t = v.daily_digest_text(rows, stats, {"equity": 5010.0, "position": None})
    assert "2건" in t and "50.0%" in t and "+10.00" in t
    assert "최고 +30.00 / 최저 -20.00" in t
    assert "익절" in t and "ST전환" in t              # 사유 한글 라벨
    assert "롱 65,000.00→65,300.00 +30.00" in t


def test_daily_digest_caps_trade_list():
    rows = [_row(pnl=float(i)) for i in range(20)]
    stats = {"n": 20, "wins": 19, "winRate": 95.0, "totalPnl": 190.0, "avgPnl": 9.5,
             "profitFactor": 99.0, "maxDrawdown": 0.0}
    t = v.daily_digest_text(rows, stats, {"equity": 100.0})
    assert "…외 5건" in t                             # 20건 중 15만 나열


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
