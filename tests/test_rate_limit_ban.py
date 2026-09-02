"""레이트리밋 밴 — 밴 중에는 요청을 보내지 않는다. 네트워크 없음.

실측 사고(2026-09-02): 바이낸스가 IP 를 밴했다.
  {"code":-1003,"msg":"Way too many requests; IP(...) banned until 1788319856878.
   Please use the websocket for live updates to avoid bans."}
밴 상태에서 청산 재시도가 **매 폴 79요청**씩 누적됐고(-2022 실패 → 다음 폴 재시도), 결국
11분간 봇이 무응답이 됐다. **밴 중에 계속 때리면 바이낸스가 밴을 연장한다** — 스스로 상처를
키우는 짓이라, 만료까지 쉬는 게 유일한 대응이다.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.binance_broker import BinanceBroker, RateLimited, _ban_until   # noqa: E402


BAN_MSG = ('binanceusdm {"code":-1003,"msg":"Way too many requests; '
           'IP(15.158.3.43) banned until 1788319856878. '
           'Please use the websocket for live updates to avoid bans."}')


def test_parses_the_ban_expiry_from_the_real_message():
    """실제로 받은 메시지에서 만료 시각을 뽑아내야 한다 — 못 읽으면 얼마나 쉴지 모른다."""
    assert _ban_until(BAN_MSG) == 1788319856878


def test_unparseable_ban_still_backs_off():
    """만료 시각이 없어도 밴이면 쉰다 — 보수적으로 기본 대기."""
    until = _ban_until('binanceusdm 418 {"code":-1003,"msg":"Way too many requests"}')
    assert until is not None and until > time.time() * 1000


def test_ordinary_errors_are_not_bans():
    """-2022 나 일반 오류를 밴으로 오인하면 멀쩡한 봇이 멈춰 선다."""
    assert _ban_until('{"code":-2022,"msg":"ReduceOnly Order is rejected."}') is None
    assert _ban_until("connection reset by peer") is None


def _future_ban_msg(secs=600):
    """만료가 **미래**인 밴 메시지. BAN_MSG 의 시각은 실제 사고 시점이라 이미 지났다 —
    그걸 차단 테스트에 쓰면 '만료됨'으로 통과해 테스트가 조용히 무의미해진다."""
    until = int(time.time() * 1000) + secs * 1000
    return f'binanceusdm {{"code":-1003,"msg":"Way too many requests; IP(1.2.3.4) banned until {until}."}}', until


class _Boom:
    """첫 호출에서 밴을 던지는 가짜 ccxt 클라이언트. 이후 호출 횟수를 센다."""

    def __init__(self, msg):
        self.calls = 0
        self.msg = msg

    def fetch_balance(self, *a, **k):
        self.calls += 1
        raise Exception(self.msg)


def _broker(msg=None):
    b = BinanceBroker("k", "s", True, "BTCUSDT")
    raw = _Boom(msg or BAN_MSG)
    from engine.binance_broker import _Guarded
    b._ex, b._guard = raw, _Guarded(raw, b)
    return b, raw


def test_ban_is_recorded_and_further_requests_never_leave_the_process():
    """★ 핵심 — 밴을 만난 뒤에는 **네트워크를 아예 안 탄다.** 계속 때리면 밴이 연장된다."""
    msg, until = _future_ban_msg()
    b, raw = _broker(msg)
    try:
        b.client().fetch_balance()
        assert False, "RateLimited 가 올라와야 한다"
    except RateLimited as e:
        assert e.until_ms == until
    assert raw.calls == 1

    for _ in range(5):                       # 밴 중 재시도
        try:
            b.client().fetch_balance()
            assert False
        except RateLimited:
            pass
    assert raw.calls == 1, f"밴 중에 요청이 나갔다 — {raw.calls}회"


def test_ban_clears_when_it_expires():
    """만료되면 스스로 풀려야 한다 — 안 그러면 봇이 영영 안 돈다."""
    b, raw = _broker()
    b._banned_until = int(time.time() * 1000) - 1     # 이미 지난 밴
    b.raise_if_banned()                               # 예외 없이 통과
    assert b._banned_until == 0
