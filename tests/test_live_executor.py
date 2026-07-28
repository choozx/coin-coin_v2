"""LiveExecutor(실거래) — 가짜 브로커로 실주문 경로 전체를 네트워크 없이 검증.

여기서 지키려는 것은 하나다: **엔진이 가정한 가격이 아니라 실제 체결로 포지션이 잡히는가.**
백테스트는 '신호봉 종가에 원하는 수량이 다 체결된다'고 낙관하지만 실거래는 안 그렇다 —
그 차이를 executor 가 흡수하지 못하면 이후 손절·청산 판정이 전부 허구 위에서 돈다.

가짜 브로커는 binance_broker.BinanceBroker 와 같은 표면만 흉내낸다(ccxt·네트워크 없음).
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.backtest import _Position                       # noqa: E402
from engine.binance_broker import Fill, OrderError, _merge   # noqa: E402
from engine.executor import LiveExecutor, api_keys           # noqa: E402
from engine.live import LiveTrader                          # noqa: E402


def _with_env(**kv):
    """환경변수를 잠깐 바꿨다 되돌리는 컨텍스트(테스트끼리 안 새게)."""
    import contextlib

    @contextlib.contextmanager
    def cm():
        old = {k: os.environ.get(k) for k in kv}
        try:
            for k, v in kv.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            yield
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    return cm()


class FakeBroker:
    """주문을 받아 정해둔 체결을 돌려주는 가짜 거래소."""

    def __init__(self, fills=None, position=None, equity=1000.0,
                 min_qty=0.001, min_cost=5.0, step=0.001):
        self.fills = list(fills or [])       # 순서대로 소비. 비면 요청 수량 그대로 체결
        self.position_data = position
        self._equity = equity
        self.min_qty, self.min_cost, self.step = min_qty, min_cost, step
        self.orders = []                     # (kind, side, qty, reduce_only)
        self.leverage = None
        self.hedge = False
        self.maker_attempts = None           # 마지막 limit 주문에 넘어온 재호가 횟수

    # -- 메타/조회 --
    def market(self):
        return {"symbol": "BTC/USDT:USDT", "quote": "USDT",
                "limits": {"amount": {"min": self.min_qty}, "cost": {"min": self.min_cost}}}

    def round_qty(self, qty):
        return round(int(qty / self.step) * self.step, 8)

    def round_price(self, p):
        return round(p, 2)

    def check_order_size(self, qty, price):
        if qty <= 0:
            raise OrderError("수량 0")
        if qty < self.min_qty:
            raise OrderError(f"최소 수량 미달 {qty}")
        if qty * price < self.min_cost:
            raise OrderError(f"최소 명목가 미달 {qty * price}")

    def equity(self, asset):
        return self._equity

    def position(self):
        return self.position_data

    def set_leverage(self, lev):
        self.leverage = lev

    def funding_paid(self, a, b):
        return 0.0

    def ensure_isolated(self):
        return "isolated"

    def position_mode(self):
        return self.hedge          # True=헤지 모드 → preflight 가 거부해야 한다

    # -- 체결 --
    def _next(self, kind, side, qty, reduce_only):
        self.orders.append((kind, side, qty, reduce_only))
        if self.fills:
            return self.fills.pop(0)
        return Fill(price=100.0, qty=qty, taker_qty=qty, fee=None, ts=1)

    def market_order(self, side, qty, reduce_only=False):
        return self._next("market", side, qty, reduce_only)

    def limit_then_market(self, side, qty, timeout_s, reduce_only=False, max_attempts=1):
        self.maker_attempts = max_attempts
        return self._next("limit", side, qty, reduce_only)


def _near(a, b, tol=1e-9):
    return abs(float(a) - float(b)) <= tol + 1e-6 * abs(float(b))


def _raises(exc, fn):
    try:
        fn()
    except exc:
        return
    raise AssertionError(f"{exc.__name__} 가 나야 하는데 안 남")


def _pos(side=1, price=100.0, qty=1.0, lev=5):
    return _Position(side=side, entry_time=1_000, entry_price=price, qty=qty, leverage=lev,
                     margin=price * qty / lev, liq_price=80.0, stop_price=95.0, tp_price=110.0,
                     entry_fee=0.05, entry_signal_idx=3, peak=price)


def _ex(broker, **kw):
    kw.setdefault("position_path", os.path.join(tempfile.mkdtemp(prefix="livepos-"), "p.json"))
    return LiveExecutor(testnet=True, symbol="BTCUSDT", broker=broker, **kw)


def test_open_adopts_real_fill_not_engine_assumption():
    """체결가·수량·수수료가 엔진 가정과 다르면 실제 값으로 포지션이 잡혀야 한다."""
    broker = FakeBroker(fills=[Fill(price=100.4, qty=0.9, taker_qty=0.9, fee=0.45)],
                        position={"side": 1, "qty": 0.9, "entry_price": 100.4, "leverage": 5,
                                  "liq_price": 81.7, "margin": 18.07})
    ex = _ex(broker)
    p = _pos(price=100.0, qty=1.0)
    ex.open(p)
    assert ex.position is p
    assert p.entry_price == 100.4 and p.qty == 0.9      # 슬리피지·부분체결 반영
    assert p.entry_fee == 0.45                          # 거래소가 알려준 실수수료
    assert p.liq_price == 81.7                          # 청산가는 거래소 계산값을 채택
    assert broker.leverage == 5
    assert broker.orders[0][:3] == ("market", "buy", 1.0)   # 주문은 1.0, 체결은 0.9


def test_open_uses_post_only_path_when_maker():
    """maker 진입 프리셋이면 post-only 지정가(→3초 후 시장가) 경로를 타야 한다."""
    broker = FakeBroker()
    ex = _ex(broker)
    ex.open(_pos(), is_maker=True)
    assert broker.orders[0][0] == "limit"
    assert broker.orders[0][3] is False           # 진입은 reduceOnly 아님


def test_maker_open_threads_reprice_attempts_to_broker():
    """maker 진입이면 재호가 횟수(기본 5)가 브로커까지 전달돼야 한다."""
    broker = FakeBroker()
    ex = _ex(broker)
    assert ex.maker_max_attempts == 5              # env 없으면 기본 5
    ex.open(_pos(), is_maker=True)
    assert broker.maker_attempts == 5


def test_maker_max_attempts_env_override():
    """MAKER_MAX_ATTEMPTS 로 재호가 횟수를 바꿀 수 있다(1=구 동작)."""
    with _with_env(MAKER_MAX_ATTEMPTS="1"):
        broker = FakeBroker()
        ex = _ex(broker)
        assert ex.maker_max_attempts == 1
        ex.open(_pos(), is_maker=True)
        assert broker.maker_attempts == 1


def test_open_rejected_below_min_notional_leaves_no_position():
    """최소주문 미달이면 포지션이 생기면 안 된다(주문도 안 나가야 한다)."""
    ex = _ex(FakeBroker(min_cost=100.0))
    _raises(OrderError, lambda: ex.open(_pos(price=100.0, qty=0.5)))   # 명목 50 < 100
    assert ex.position is None
    assert ex.broker.orders == []


def test_close_records_real_exit_price_and_fee():
    broker = FakeBroker()
    ex = _ex(broker)
    ex.open(_pos(price=100.0, qty=1.0))              # 기본 체결: 100.0 x1, 수수료 미확정
    broker.fills.append(Fill(price=94.2, qty=1.0, taker_qty=1.0, fee=0.47))
    tr = ex.close(95.0, "stop_loss", 2_000)          # 엔진은 95 를 가정했지만 실제론 94.2
    assert tr.exit_price == 94.2                     # 손절 슬리피지가 그대로 기록
    assert tr.exit_reason == "stop_loss"
    assert broker.orders[-1] == ("market", "sell", 1.0, True)   # 손절은 시장가·reduceOnly
    entry_fee = 100.0 * 1.0 * ex.taker_fee           # fee=None → 공식 근사
    assert _near(tr.fees, entry_fee + 0.47)
    assert _near(tr.pnl, -5.8 - tr.fees)
    assert ex.position is None


def test_liquidation_uses_exchange_truth():
    """엔진이 '강제청산' 판정했는데 거래소엔 포지션이 없으면 주문을 내면 안 된다."""
    broker = FakeBroker()
    ex = _ex(broker)
    ex.open(_pos())
    broker.position_data = None                      # 거래소: 이미 털림
    n = len(broker.orders)
    tr = ex.close(80.0, "liquidation", 3_000)
    assert len(broker.orders) == n                   # 추가 주문 없음
    assert tr.exit_reason == "liquidation" and tr.exit_price == 80.0


def test_liquidation_still_open_falls_back_to_market_close():
    """반대로 아직 살아 있으면 시장가로 확실히 빠져나와야 한다(추정 청산가가 빗나간 경우)."""
    broker = FakeBroker(position={"side": 1, "qty": 1.0, "entry_price": 100.0, "leverage": 5,
                                  "liq_price": 79.0, "margin": 20.0})
    ex = _ex(broker)
    ex.open(_pos())
    broker.fills.append(Fill(price=79.5, qty=1.0, taker_qty=1.0, fee=0.4))
    tr = ex.close(80.0, "liquidation", 3_000)
    assert broker.orders[-1] == ("market", "sell", 1.0, True)
    assert tr.exit_price == 79.5


def test_partial_close_keeps_remainder_and_raises():
    """부분청산이면 잔량을 계속 들고 있어야 한다 — '다 닫았다'고 기록하면 유령 포지션이 남는다."""
    broker = FakeBroker()
    ex = _ex(broker)
    ex.open(_pos(qty=1.0))
    broker.fills.append(Fill(price=99.0, qty=0.4, taker_qty=0.4, fee=0.2))
    _raises(OrderError, lambda: ex.close(99.0, "signal", 2_000))
    assert ex.position is not None
    assert _near(ex.position.qty, 0.6)
    assert ex.trades == []


def test_position_sidecar_roundtrip():
    """재시작 복원용 사이드카: 진입 때 쓰고 청산 때 지운다."""
    broker = FakeBroker()
    ex = _ex(broker)
    ex.open(_pos(side=-1, price=100.0, qty=1.0))
    saved = ex.load_saved_position()
    assert saved["side"] == -1 and saved["stop"] == 95.0 and saved["symbol"] == "BTCUSDT"
    ex.close(99.0, "signal", 2_000)
    assert ex.load_saved_position() == {}


def test_unrealized_pnl_marks_to_last_close_as_roe():
    """보유 포지션의 미실현손익 = 마지막 종가 기준 gross, 수익률은 증거금 대비 ROE(레버리지 반영)."""
    from engine.live import LiveTrader

    class S:
        _last_price = 110.0
    s = S()
    long = _pos(side=1, price=100.0, qty=1.0, lev=5)      # margin = 100*1/5 = 20
    u = LiveTrader._unrealized(s, long)
    assert u["mark"] == 110.0
    assert _near(u["uPnl"], 10.0)                          # (110-100)*1
    assert _near(u["uPnlPct"], 50.0)                       # 10/20*100 — 5x 라 가격 +10%가 ROE +50%
    short = _pos(side=-1, price=100.0, qty=1.0, lev=5)
    assert _near(LiveTrader._unrealized(s, short)["uPnl"], -10.0)   # 숏은 오르면 손실

    class S0:
        _last_price = None                                 # 첫 폴 전 → 현재가 미확정
    assert LiveTrader._unrealized(S0(), long) == {"mark": None, "uPnl": None, "uPnlPct": None}


def test_merge_fills_weighted_average():
    a = Fill(price=100.0, qty=0.5, maker_qty=0.5, fee=0.01)
    b = Fill(price=102.0, qty=0.5, taker_qty=0.5, fee=0.05)
    m = _merge(a, b)
    assert _near(m.price, 101.0) and _near(m.qty, 1.0)
    assert _near(m.fee, 0.06) and not m.is_maker


def test_equity_is_cached_but_invalidated_by_trades():
    broker = FakeBroker(equity=500.0)
    ex = _ex(broker)
    assert ex.equity() == 500.0
    broker._equity = 700.0
    assert ex.equity() == 500.0                      # 캐시(3초)
    ex.open(_pos())
    assert ex.equity() == 700.0                      # 매매가 있었으면 즉시 재조회


def test_symbol_change_rebinds_and_is_blocked_while_holding():
    """심볼이 바뀌면 마진자산도 따라가야 하고, 포지션 보유 중엔 아예 막혀야 한다."""
    ex = _ex(FakeBroker())
    ex.set_symbol("ETHUSDC")
    assert ex.symbol == "ETHUSDC" and ex.quote_asset == "USDC"
    holding = _ex(FakeBroker())
    holding.open(_pos())
    _raises(RuntimeError, lambda: holding.set_symbol("ETHUSDT"))
    assert holding.symbol == "BTCUSDT"


# ---- 폴 루프 중 거래소 포지션 재조정 (유령 포지션 방지) --------------------
# 재시작 때만 맞추면, 도는 중에 봇 모르게 강제청산·수동청산·수동진입·부분축소가 나도 엔진이
# 유령 포지션 위에서 판정을 계속한다. _reconcile_live_position 이 매 폴 그 갭을 닫는다.

class _FakeBase:
    """open_time 만 있으면 되는 초경량 캔들 대역(_external_close 가 마지막 봉 시각만 씀)."""
    def __init__(self, times):
        self.open_time = times

    def __len__(self):
        return len(self.open_time)


class _RecEx:
    """reconcile 라우팅용 가짜 executor — sync_position/position/_save_position 만."""
    def __init__(self, xpos, local, fail=False):
        self._xpos, self.position, self._fail = xpos, local, fail
        self.saved = 0

    def sync_position(self):
        if self._fail:
            raise RuntimeError("조회 실패")
        return self._xpos

    def _save_position(self):
        self.saved += 1


class _LP:
    """로컬 포지션 대역(side/qty 만 본다)."""
    def __init__(self, side, qty):
        self.side, self.qty = side, qty


class _RecTrader:
    """reconcile 라우팅만 확인 — 하위 동작(_external_close/_sync_live_position)은 호출만 기록."""
    def __init__(self, ex, is_live=True):
        self.is_live, self.ex = is_live, ex
        self._last_price, self._flat_divergence, self.calls = 95.0, 0, []

    def _external_close(self, pos, base):
        self.calls.append("external_close")

    def _sync_live_position(self, base, context="재시작"):
        self.calls.append(f"sync:{context}")


def _reconcile(rec, base=None):
    LiveTrader._reconcile_live_position(rec, base)


def _xp(side=1, qty=1.0, price=100.0):
    return {"side": side, "qty": qty, "entry_price": price, "leverage": 5,
            "liq_price": 80.0, "margin": 20.0}


def test_reconcile_flat_divergence_needs_two_polls():
    """거래소 무포지션인데 로컬 보유 — 1폴은 관망, 2폴 연속이어야 외부청산 기록(순간 조회오류 방어)."""
    rec = _RecTrader(_RecEx(xpos=None, local=_LP(1, 1.0)))
    _reconcile(rec)
    assert rec._flat_divergence == 1 and rec.calls == []      # 1회차: 관망
    _reconcile(rec)
    assert rec.calls == ["external_close"] and rec._flat_divergence == 0


def test_reconcile_transient_flat_does_not_close():
    """1폴 플랫이었다가 다음 폴에 포지션이 다시 보이면 외부청산하지 않는다(디바운스 리셋)."""
    ex = _RecEx(xpos=None, local=_LP(1, 1.0))
    rec = _RecTrader(ex)
    _reconcile(rec)                                           # div=1
    ex._xpos = _xp(1, 1.0)                                    # 거래소에 다시 보임(조회오류였을 뿐)
    _reconcile(rec)
    assert rec.calls == [] and rec._flat_divergence == 0      # 기록 안 함


def test_reconcile_adopts_untracked_position():
    """거래소 보유 + 로컬 무포지션 → 추적 안 되던 포지션 인계."""
    rec = _RecTrader(_RecEx(xpos=_xp(1, 1.0), local=None))
    _reconcile(rec)
    assert rec.calls == ["sync:동기화"]


def test_reconcile_adjusts_qty_on_partial_change():
    """방향 같고 수량만 어긋나면(>2%) 거래소 수량으로 맞추고 사이드카 갱신."""
    import engine.live as live
    local = _LP(1, 1.0)
    ex = _RecEx(xpos=_xp(1, 1.5), local=local)
    rec = _RecTrader(ex)
    orig, sent = live.notify, []
    live.notify = lambda m: sent.append(m)
    try:
        _reconcile(rec)
    finally:
        live.notify = orig
    assert local.qty == 1.5 and ex.saved == 1 and rec.calls == []
    assert any("수량 조정" in m for m in sent)


def test_reconcile_ignores_qty_within_tolerance():
    """2% 이내 차이는 반올림·정밀도 노이즈로 보고 건드리지 않는다."""
    local = _LP(1, 1.0)
    rec = _RecTrader(_RecEx(xpos=_xp(1, 1.01), local=local))
    _reconcile(rec)
    assert local.qty == 1.0 and rec.ex.saved == 0 and rec.calls == []


def test_reconcile_reinherits_on_side_flip():
    """방향까지 다르면 통째로 재인계(로컬을 비우고 거래소 기준으로)."""
    import engine.live as live
    ex = _RecEx(xpos=_xp(-1, 1.0), local=_LP(1, 1.0))
    rec = _RecTrader(ex)
    orig = live.notify
    live.notify = lambda m: None
    try:
        _reconcile(rec)
    finally:
        live.notify = orig
    assert ex.position is None and rec.calls == ["sync:동기화"]


def test_reconcile_skips_on_fetch_failure():
    """거래소 조회가 실패하면 이번 폴은 손대지 않는다(모르는 채 지우지 않기)."""
    local = _LP(1, 1.0)
    rec = _RecTrader(_RecEx(xpos=None, local=local, fail=True))
    _reconcile(rec)
    assert rec.calls == [] and rec._flat_divergence == 0 and rec.ex.position is local


def test_reconcile_noop_when_in_sync():
    """양쪽이 일치하면 아무 일도 없다(가장 흔한 경로)."""
    rec = _RecTrader(_RecEx(xpos=_xp(1, 1.0), local=_LP(1, 1.0)))
    _reconcile(rec)
    assert rec.calls == [] and rec._flat_divergence == 0


def test_reconcile_skips_paper_mode():
    """페이퍼는 거래소가 없으니 재조정을 건너뛴다."""
    ex = _RecEx(xpos=None, local=_LP(1, 1.0))
    rec = _RecTrader(ex, is_live=False)
    _reconcile(rec)
    assert rec.calls == [] and rec._flat_divergence == 0      # sync_position 도 안 부름


def test_external_close_records_ledger_and_clears_position():
    """봇 몰래 사라진 포지션 → 마지막 종가로 external 청산 기록 + 로컬 정리 + 알림."""
    import engine.live as live
    broker = FakeBroker()
    ex = _ex(broker)
    ex.open(_pos(price=100.0, qty=1.0))               # 포지션 보유 + 사이드카 기록
    broker.position_data = None                       # 거래소는 이미 플랫

    class T:
        pass
    t = T()
    t.ex, t._last_price = ex, 95.0
    t.preset = type("P", (), {"name": "슈퍼", "symbol": "BTCUSDT", "timeframe": "15m"})()
    t.strategy_path, t.mode = None, "testnet"
    t.ledger_path = os.path.join(tempfile.mkdtemp(prefix="ledger-"), "t.db")
    base = _FakeBase([1_000, 2_000])
    orig, sent = live.notify, []
    live.notify = lambda m: sent.append(m)
    try:
        LiveTrader._external_close(t, ex.position, base)
    finally:
        live.notify = orig

    assert ex.position is None
    assert len(ex.trades) == 1
    tr = ex.trades[-1]
    assert tr.exit_reason == "external" and tr.exit_price == 95.0 and tr.exit_time == 2_000
    from engine import ledger
    rows = ledger.load(t.ledger_path, mode="testnet")
    assert len(rows) == 1 and rows[0]["reason"] == "external"
    assert any("외부 청산" in m for m in sent)


# ---- 거래소 네트워크 전환 (테스트넷 ↔ 실돈) --------------------------------
# 전환은 플래그 뒤집기가 아니라 '다른 계정으로 이사'다. 잘못 새면 가짜돈 전략이 실돈에 나가거나,
# 반쯤 바뀐 상태로 매매가 이어진다. 아래가 그 두 가지를 막는다.

def test_api_keys_prefer_network_specific_then_fall_back_to_shared():
    with _with_env(BINANCE_API_KEY="shared", BINANCE_API_SECRET="ss",
                   BINANCE_TESTNET_API_KEY="tk", BINANCE_TESTNET_API_SECRET="ts",
                   BINANCE_MAINNET_API_KEY=None, BINANCE_MAINNET_API_SECRET=None):
        assert api_keys(testnet=True) == ("tk", "ts")        # 전용 키 우선
        assert api_keys(testnet=False) == ("shared", "ss")   # 없으면 공용으로 폴백


def test_switch_to_mainnet_needs_real_money_grant():
    """--real-money 없이 뜬 봇은 대시보드 버튼으로도 실돈에 못 간다."""
    ex = _ex(FakeBroker())                                   # allow_mainnet 기본 False
    _raises(RuntimeError, lambda: ex.set_network("mainnet"))
    assert ex.network == "testnet"


def test_switch_network_blocked_while_holding_position():
    ex = _ex(FakeBroker(), allow_mainnet=True)
    ex.open(_pos())
    _raises(RuntimeError, lambda: ex.set_network("mainnet"))
    assert ex.network == "testnet"


def test_failed_switch_rolls_back_to_previous_network():
    """새 계정 점검(preflight)이 실패하면 반쯤 바뀐 채로 두지 말고 원래대로 되돌려야 한다."""
    broker = FakeBroker()
    broker.hedge = True                                      # 헤지 모드 계정 → preflight 거부
    ex = _ex(broker, allow_mainnet=True)
    with _with_env(BINANCE_API_KEY="k", BINANCE_API_SECRET="s"):
        _raises(RuntimeError, lambda: ex.set_network("mainnet"))
    assert ex.network == "testnet" and ex.testnet is True


def test_successful_switch_flips_network_and_keys():
    broker = FakeBroker()
    ex = _ex(broker, allow_mainnet=True)
    with _with_env(BINANCE_API_KEY=None, BINANCE_API_SECRET=None,
                   BINANCE_MAINNET_API_KEY="mk", BINANCE_MAINNET_API_SECRET="ms"):
        ex.set_network("mainnet")
    assert ex.network == "mainnet" and ex.testnet is False
    assert (ex.api_key, ex.api_secret) == ("mk", "ms")


def test_ledger_buckets_never_merge():
    """페이퍼·테스트넷·실돈이 한 원장 버킷에 섞이면 실돈 수익률에 가짜돈 손익이 들어간다."""
    class Stub:
        pass
    s = Stub()
    s.is_live, s.ex = False, None
    assert LiveTrader._ledger_mode(s) == "paper"
    s.is_live, s.ex = True, _ex(FakeBroker())                # 테스트넷 실거래
    assert LiveTrader._ledger_mode(s) == "testnet"
    s.ex.testnet = False
    assert LiveTrader._ledger_mode(s) == "live"


def test_testnet_client_turns_off_ccxt_sandbox_guard():
    """ccxt 4.5+ 는 테스트넷의 private 엔드포인트 호출을 NotSupported 로 막아버린다.

    바이낸스가 옛 선물 테스트넷을 Demo Trading 으로 밀면서 ccxt 가 붙인 경고인데,
    엔드포인트는 멀쩡히 산다. 이 가드를 끄지 않으면 잔고 조회부터 실패해서
    **실주문 경로를 실돈 말고는 시험할 방법이 없어진다.** 그래서 테스트넷일 때만 끈다.
    """
    from engine.binance_broker import BinanceBroker
    try:
        t = BinanceBroker("k", "s", testnet=True, symbol="BTCUSDT").client()
        m = BinanceBroker("k", "s", testnet=False, symbol="BTCUSDT").client()
    except RuntimeError as e:
        if "ccxt" in str(e):
            print("   (ccxt 미설치 — 스킵)")
            return
        raise
    assert t.options.get("disableFuturesSandboxWarning") is True
    assert "testnet" in t.urls["api"]["fapiPrivate"]
    assert not m.options.get("disableFuturesSandboxWarning")   # 메인넷엔 불필요
    assert "testnet" not in m.urls["api"]["fapiPrivate"]


def test_notify_sets_user_agent_and_right_payload_key():
    """Discord 앞단 Cloudflare 는 파이썬 기본 UA 를 403(1010)으로 막는다.

    헤더 하나가 빠져서 알림이 통째로 안 왔는데, notify 가 예외를 삼켜 실패한 줄도 몰랐다.
    UA 를 붙이는 것과, 웹훅 종류별 payload 키(Slack=text / Discord=content)를 함께 못박는다.
    """
    import engine.notifier as notifier          # notify 는 이제 engine.notifier 에 있다
    sent = []

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    orig = notifier.urllib.request.urlopen
    notifier.urllib.request.urlopen = lambda req, timeout=None: sent.append(req) or FakeResp()
    try:
        # 봇 토큰이 없어야 웹훅 경로를 탄다 — 환경에 새어 있을 수 있으니 명시적으로 끈다.
        with _with_env(DISCORD_BOT_TOKEN=None, DISCORD_CHANNEL_ID=None,
                       NOTIFY_WEBHOOK="https://discord.com/api/webhooks/1/abc"):
            notifier.notify("hi")
        with _with_env(DISCORD_BOT_TOKEN=None, DISCORD_CHANNEL_ID=None,
                       NOTIFY_WEBHOOK="https://hooks.slack.com/services/x"):
            notifier.notify("hi")
    finally:
        notifier.urllib.request.urlopen = orig

    assert len(sent) == 2
    for req in sent:
        ua = req.get_header("User-agent") or ""
        assert ua and "urllib" not in ua.lower(), f"UA 없음/기본값: {ua!r}"
        assert req.get_header("Authorization") is None    # 웹훅엔 인증 헤더 없음
    import json as _j
    assert "content" in _j.loads(sent[0].data)      # Discord
    assert "text" in _j.loads(sent[1].data)         # Slack


def test_notify_prefers_bot_token_over_webhook():
    """봇 토큰+채널이 있으면 웹훅 대신 봇 채널 REST 로 보낸다(Authorization: Bot, 채널 엔드포인트)."""
    import engine.notifier as notifier
    import json as _j
    sent = []

    class FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    orig = notifier.urllib.request.urlopen
    notifier.urllib.request.urlopen = lambda req, timeout=None: sent.append(req) or FakeResp()
    try:
        with _with_env(DISCORD_BOT_TOKEN="tok123", DISCORD_CHANNEL_ID="999",
                       NOTIFY_WEBHOOK="https://discord.com/api/webhooks/1/abc"):  # 있어도 봇이 이긴다
            notifier.notify("hi")
    finally:
        notifier.urllib.request.urlopen = orig

    assert len(sent) == 1
    req = sent[0]
    assert req.full_url == "https://discord.com/api/v10/channels/999/messages"
    assert req.get_header("Authorization") == "Bot tok123"
    assert req.get_header("User-agent") and "urllib" not in req.get_header("User-agent").lower()
    assert _j.loads(req.data)["content"] == "hi"


def test_notify_silent_when_nothing_configured():
    """봇도 웹훅도 없으면 조용히 아무것도 안 보낸다(설정 안 했으면 무동작)."""
    import engine.notifier as notifier
    sent = []
    orig = notifier.urllib.request.urlopen
    notifier.urllib.request.urlopen = lambda req, timeout=None: sent.append(req)
    try:
        with _with_env(DISCORD_BOT_TOKEN=None, DISCORD_CHANNEL_ID=None, NOTIFY_WEBHOOK=None):
            notifier.notify("hi")
    finally:
        notifier.urllib.request.urlopen = orig
    assert sent == []


def test_notify_failure_is_logged_not_swallowed(capsys=None):
    """웹훅이 죽어도 매매는 계속돼야 하지만, 로그에는 남아야 한다(조용한 실패 방지)."""
    import io, contextlib
    import engine.notifier as notifier
    orig = notifier.urllib.request.urlopen

    def boom(req, timeout=None):
        raise OSError("network down")

    notifier.urllib.request.urlopen = boom
    buf = io.StringIO()
    try:
        with _with_env(DISCORD_BOT_TOKEN=None, DISCORD_CHANNEL_ID=None,
                       NOTIFY_WEBHOOK="https://discord.com/api/webhooks/1/abc"):
            with contextlib.redirect_stdout(buf):
                notifier.notify("hi")            # 예외가 밖으로 나가면 안 됨
    finally:
        notifier.urllib.request.urlopen = orig
    assert "알림 실패" in buf.getvalue()


def test_pause_resume_notifies_only_on_change():
    """멈춤/재개는 '바뀐 순간'에만 알려야 한다 — 폴링(1분)마다 보내면 알림이 쓸모없어진다."""
    import engine.live as live

    class Stub:
        _paused = True                      # 안전 시작 = 멈춤 상태로 떠 있음
        preset = type("P", (), {"symbol": "BTCUSDC", "timeframe": "15m"})()

    sent, state = [], {"v": "paused"}
    orig_notify, orig_svc = live.notify, live.control.service_state
    live.notify = lambda m: sent.append(m)
    live.control.service_state = lambda *a, **k: state["v"]
    try:
        s = Stub()
        live.LiveTrader._sync_paused(s)                 # 변화 없음
        assert sent == [], "안 바뀌었는데 알림이 갔다"
        state["v"] = "running"
        live.LiveTrader._sync_paused(s)                 # 재개
        live.LiveTrader._sync_paused(s)                 # 그대로 → 조용해야
        assert len(sent) == 1 and "재개" in sent[0], sent
        assert s._paused is False
        state["v"] = "paused"
        live.LiveTrader._sync_paused(s)                 # 다시 멈춤
        assert len(sent) == 2 and "멈춤" in sent[1], sent
    finally:
        live.notify, live.control.service_state = orig_notify, orig_svc


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
