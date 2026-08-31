"""체결 실측 로그 — 기대 vs 실제, 슬리피지 부호, maker 비율. 네트워크 없음.

배경: LiveExecutor.open() 은 실제 체결로 포지션을 덮어쓴다(pos.entry_price = fill.price).
그 순간 엔진이 기대했던 가격이 사라져서, 며칠을 돌려도 "슬리피지가 얼마였나"에 답할 수
없었다. 원장에도 maker/taker 구분이 없다. 테스트넷 실전 검증의 측정 대상이 바로 그 둘이다.

가장 쉽게 틀리는 곳은 **슬리피지 부호**다. raw 차이를 그냥 평균 내면 롱·숏이 상쇄돼
'슬리피지 0' 이라는 거짓말이 나온다 — 그래서 '불리한 방향'으로 부호를 통일한다.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import fill_log                      # noqa: E402
from engine.binance_broker import Fill           # noqa: E402


# ---- 슬리피지 부호: 양수 = 손해 ----

def test_buy_paying_more_is_adverse():
    """진입 롱은 매수 — 기대보다 비싸게 사면 손해(+)."""
    assert fill_log.adverse_pct("entry", 1, 100.0, 100.1) > 0
    assert fill_log.adverse_pct("entry", 1, 100.0, 99.9) < 0        # 싸게 샀으면 이득


def test_sell_getting_less_is_adverse():
    """진입 숏은 매도 — 기대보다 싸게 팔면 손해(+)."""
    assert fill_log.adverse_pct("entry", -1, 100.0, 99.9) > 0
    assert fill_log.adverse_pct("entry", -1, 100.0, 100.1) < 0


def test_exit_flips_the_side():
    """청산은 방향이 뒤집힌다 — 롱 청산은 매도, 숏 청산은 매수."""
    assert fill_log.is_buy("entry", 1) and not fill_log.is_buy("exit", 1)
    assert fill_log.is_buy("exit", -1) and not fill_log.is_buy("entry", -1)
    assert fill_log.adverse_pct("exit", 1, 100.0, 99.9) > 0         # 롱 청산을 싸게 팔았다
    assert fill_log.adverse_pct("exit", -1, 100.0, 100.1) > 0       # 숏 청산을 비싸게 샀다


def test_long_and_short_adverse_do_not_cancel_out():
    """★ 부호를 통일하지 않으면 롱·숏이 상쇄돼 '슬리피지 0' 이 나온다 — 이걸 막는 게 요점."""
    long_slip = fill_log.adverse_pct("entry", 1, 100.0, 100.1)      # 비싸게 매수
    short_slip = fill_log.adverse_pct("entry", -1, 100.0, 99.9)     # 싸게 매도
    assert long_slip > 0 and short_slip > 0
    assert (long_slip + short_slip) / 2 > 0                          # 평균이 살아남는다


def test_adverse_is_none_without_expected():
    assert fill_log.adverse_pct("entry", 1, 0, 100.0) is None
    assert fill_log.adverse_pct("entry", 1, None, 100.0) is None


# ---- 기록 내용 ----

def _fill(price=100.1, qty=1.0, maker=0.6, taker=0.4, fee=0.05):
    return Fill(price=price, qty=qty, maker_qty=maker, taker_qty=taker, fee=fee,
                order_ids=["a", "b"], ts=1_700_000_000_000)


def test_build_captures_what_the_verification_needs():
    """테스트넷 검증이 요구하는 세 값: 슬리피지 · maker 비율 · 실수수료."""
    rec = fill_log.build("entry", "BTCUSDT", 1, 100.0, 1.0, _fill(),
                         intended_maker=True, network="testnet", at_ms=1)
    assert rec["slipPct"] > 0                       # 100.0 기대 → 100.1 매수 = 손해
    assert rec["makerRatio"] == 60.0
    assert rec["fee"] == 0.05
    assert rec["feeBps"] is not None                # 명목 대비 bp — maker/taker 교차검증
    assert rec["intendedMaker"] is True             # 엔진 가정 vs 실제를 대조할 수 있어야 한다
    assert rec["orders"] == 2                       # BBO 를 몇 번 쫓았나
    assert rec["side"] == "롱" and rec["kind"] == "entry"


def test_maker_ratio_is_none_when_broker_reported_nothing():
    """체결내역 조회가 실패하면 maker/taker 가 0 이다 — 0% 로 단정하지 않고 모름으로 둔다."""
    rec = fill_log.build("entry", "BTCUSDT", 1, 100.0, 1.0,
                         _fill(maker=0, taker=0, fee=None), at_ms=1)
    assert rec["makerRatio"] is None and rec["feeBps"] is None


def test_summary_is_one_readable_line():
    rec = fill_log.build("exit", "BTCUSDT", -1, 100.0, 1.0, _fill(), reason="stop_loss", at_ms=1)
    line = fill_log.summary(rec)
    assert "청산" in line and "슬립" in line and "maker" in line


def test_record_appends_and_never_raises():
    """체결 경로에서 부른다 — 여기서 예외가 오르면 '관리되지 않는 포지션'이 생길 수 있다."""
    path = os.path.join(tempfile.mkdtemp(), "f.jsonl")
    old = fill_log.DEFAULT_PATH
    try:
        fill_log.DEFAULT_PATH = path
        fill_log.record(kind="entry", symbol="BTCUSDT", side=1, expected_price=100.0,
                        expected_qty=1.0, fill=_fill(), at_ms=1)
        assert len(fill_log.tail(path)) == 1
        assert fill_log.record(kind="entry", symbol="X", side=1, expected_price=1.0,
                               expected_qty=1.0, fill=None) == {}      # 망가진 입력도 조용히
    finally:
        fill_log.DEFAULT_PATH = old
