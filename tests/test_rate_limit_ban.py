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


# ---- 요청 계측: 밴은 '얼마나 보냈나'를 몰라서 당한다 ----

class _Counting:
    def fetch_balance(self, *a, **k): return {"total": {}}
    def fetch_positions(self, *a, **k): return []


def test_requests_are_counted_per_method():
    """폴당 요청 수를 알아야 밴 위험을 사후에라도 진단할 수 있다."""
    from engine.binance_broker import _Guarded
    b = BinanceBroker("k", "s", True, "BTCUSDT")
    raw = _Counting()
    b._ex, b._guard = raw, _Guarded(raw, b)
    b.client().fetch_balance()
    b.client().fetch_positions()
    b.client().fetch_positions()
    assert b.req_counts == {"fetch_balance": 1, "fetch_positions": 2}


def test_take_req_counts_resets_so_each_poll_is_measured_separately():
    """수확하면 리셋 — 안 그러면 누적값이라 '폴당 몇 회'를 못 읽는다."""
    from engine.binance_broker import _Guarded
    b = BinanceBroker("k", "s", True, "BTCUSDT")
    raw = _Counting()
    b._ex, b._guard = raw, _Guarded(raw, b)
    b.client().fetch_balance()
    assert b.take_req_counts() == {"fetch_balance": 1}
    assert b.take_req_counts() == {}


def test_blocked_requests_are_not_counted_as_sent():
    """밴 중 차단된 호출은 '보낸 요청'이 아니다 — 계측이 부풀면 진단이 틀어진다."""
    msg, _ = _future_ban_msg()
    b, raw = _broker(msg)
    try:
        b.client().fetch_balance()
    except RateLimited:
        pass
    sent = b.take_req_counts()
    for _ in range(3):
        try:
            b.client().fetch_balance()
        except RateLimited:
            pass
    assert b.take_req_counts() == {}, "차단된 호출이 계측에 잡히면 안 된다"
    assert sent == {"fetch_balance": 1}


def test_poll_interval_defaults_to_the_cheaper_value():
    """체결 확인 간격이 요청 수를 지배한다 — 회차당 fetch_order = TIMEOUT / POLL_SEC.

    0.4 초였을 때 청산 1회가 60~79 요청이 되어 밴을 맞았다. 이후 체결 대기를 크게 늘렸으므로
    (진입 15초 → 60초) 간격도 같이 늘려야 요청 예산이 유지된다 — 둘은 같이 움직여야 한다.
    """
    b = BinanceBroker("k", "s", True, "BTCUSDT")
    assert b.poll_interval == BinanceBroker.DEFAULT_POLL_SEC == 3.0
    assert BinanceBroker("k", "s", True, "BTCUSDT", poll_interval=0.4).poll_interval == 0.4


# ---- 밴이 매매 루프를 죽이면 안 된다 (재시작 127회 사고) ----

def test_equity_returns_last_known_value_while_banned():
    """★ 밴 중 잔고 조회 실패가 예외로 올라가면 **관찰 경로가 매매 루프를 죽인다.**

    실제 사고: _write_state → ex.equity() → RateLimited 가 **예외 핸들러 안에서** 터져
    run() 을 통째로 빠져나갔다 → 프로세스 종료 → 도커 재시작 → 반복(재시작 127회).
    """
    import tempfile
    from engine.executor import LiveExecutor

    class _Banned:
        def equity(self, asset):
            raise RateLimited(int(time.time() * 1000) + 60_000)

    ex = LiveExecutor(testnet=True, symbol="BTCUSDT", broker=_Banned(),
                      position_path=os.path.join(tempfile.mkdtemp(), "p.json"))
    ex._equity_cache = (time.time() - 999, 1234.5)      # 마지막으로 알던 값
    assert ex.equity(force=True) == 1234.5, "밴 중엔 캐시값을 돌려줘야 한다"


def test_write_state_never_raises():
    """상태 기록은 관찰용이다 — 여기서 예외가 나면 매매가 멈춘다."""
    import tempfile
    from engine.backtest import BacktestConfig
    from engine.live import LiveTrader
    from engine.executor import PaperExecutor
    from engine.preset import Preset

    tmp = tempfile.mkdtemp()
    preset = Preset({"schemaVersion": "1.0", "name": "t",
                     "market": {"exchange": "binance-futures", "symbol": "BTCUSDT",
                                "timeframe": "1m", "direction": "long"},
                     "entry": {"left": {"source": "close"}, "cmp": ">", "right": 0},
                     "exit": {"stopLoss": {"type": "percent", "value": 1.0}},
                     "sizing": {"leverage": 1, "marginMode": "isolated",
                                "size": {"type": "equityPercent", "value": 10}}})
    tr = LiveTrader(preset, PaperExecutor(equity=100.0), BacktestConfig(),
                    state_path=os.path.join(tmp, "s.json"),
                    ledger_path=os.path.join(tmp, "t.db"), strategy_path="x", mode="paper")
    tr.ex.equity = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("잔고 조회 폭발"))
    tr._write_state()                                   # 예외가 올라오면 테스트 실패
