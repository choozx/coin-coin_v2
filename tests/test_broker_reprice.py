"""BBO 재호가 체결 루프 — 실 브로커 로직을 '가짜 ccxt 클라이언트'로 검증(네트워크 없음).

test_live_executor.py 의 FakeBroker 는 limit_then_market 을 통째로 대역한다(executor 분기 검증용).
여기서는 반대로 **limit_then_market 자신**을 시험한다 — 그래서 그 안이 부르는 ccxt 표면
(fetch_order_book·create_order·fetch_order·cancel_order·fetch_my_trades)만 가짜로 끼우고,
재호가 루프·부분체결 누적·GTX 거부·taker 마무리 경로를 그대로 밟는다.

지키려는 것: maker 를 매 회차 새 BBO 로 추격하되, 소진 후 남은 수량만 taker 로 마무리하고,
회차별 부분체결이 합산 Fill 로 정확히 접히는가.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.binance_broker import BinanceBroker, OrderError    # noqa: E402


class FakeCCXT:
    """주문을 받아 '정해둔 회차별 체결'을 돌려주는 가짜 ccxt 클라이언트.

    books      : 회차별 (bid, ask) — limit 주문마다 하나씩 소비(=재호가 확인).
    limit_fills: limit 주문마다 실제 체결될 수량(요청 수량 이하). 부족분은 미체결로 남는다.
    market_px  : 시장가(taker) 체결가. remaining 을 그 가격에 전량 체결.
    reject_at  : 이 인덱스의 limit 주문은 post-only(-5022) 거부를 던진다(이미 교차).
    """

    def __init__(self, books, limit_fills, market_px=None, reject_at=()):
        self.books = books if isinstance(books, list) else [books]
        self.limit_fills = list(limit_fills)
        self.market_px = market_px
        self.reject_at = set(reject_at)
        self.orders = {}
        self.created = []            # (type, side, qty, price) 로그
        self._n = 0
        self._limit_i = 0            # 지금까지 낸 limit 주문 수(=재호가 회차 인덱스)

    def fetch_order_book(self, symbol, limit=5):
        bid, ask = self.books[min(self._limit_i, len(self.books) - 1)]
        return {"bids": [[bid, 10.0]], "asks": [[ask, 10.0]]}

    def create_order(self, symbol, type, side, qty, price, params):
        self._n += 1
        oid = f"o{self._n}"
        if type == "limit":
            idx = self._limit_i
            self._limit_i += 1
            if idx in self.reject_at:
                raise Exception("binance -5022 Post Only order will be rejected")
            filled = min(qty, self.limit_fills[idx] if idx < len(self.limit_fills) else 0.0)
            o = {"id": oid, "status": "closed" if filled >= qty - 1e-12 else "open",
                 "filled": filled, "average": price if filled > 0 else None,
                 "price": price, "amount": qty, "timestamp": 1, "type": "limit"}
        else:
            o = {"id": oid, "status": "closed", "filled": qty, "average": self.market_px,
                 "price": self.market_px, "amount": qty, "timestamp": 1, "type": "market"}
        self.orders[oid] = o
        self.created.append((type, side, qty, price))
        return dict(o)

    def fetch_order(self, oid, symbol):
        return dict(self.orders[oid])

    def cancel_order(self, oid, symbol):
        o = self.orders[oid]
        if o["status"] == "open":
            o["status"] = "canceled"
        return dict(o)

    def fetch_my_trades(self, symbol, limit=50):
        return []                    # 빈 목록 → _fill_of 가 주문타입으로 maker/taker 추정


class _Broker(BinanceBroker):
    """ccxt·load_markets 없이 도는 브로커 — client/market/정밀도만 가짜로 덮는다."""

    def __init__(self, fake):
        super().__init__("k", "s", testnet=True, symbol="BTCUSDT", poll_interval=0.005)
        self._fake = fake

    def client(self):
        return self._fake

    def market(self):
        return {"symbol": "BTC/USDT:USDT", "quote": "USDT",
                "limits": {"amount": {"min": 0.001}, "cost": {"min": 5.0}}}

    def round_qty(self, qty):
        return round(qty, 6)

    def round_price(self, price):
        return round(price, 2)


def _near(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol + 1e-6 * abs(float(b))


TIMEOUT = 0.02        # 회차당 대기 — 미체결 회차는 이만큼만 돈다(테스트 속도)


def test_full_maker_first_attempt():
    """첫 회차에 전량 체결되면 재호가도 taker 도 없어야 한다."""
    fake = FakeCCXT(books=(100.0, 101.0), limit_fills=[1.0])
    fill = _Broker(fake).limit_then_market("buy", 1.0, TIMEOUT, max_attempts=5)
    assert _near(fill.qty, 1.0) and _near(fill.maker_qty, 1.0) and _near(fill.taker_qty, 0.0)
    assert _near(fill.price, 100.0)                       # 매수는 best bid 에 붙는다
    assert [c[0] for c in fake.created] == ["limit"]      # 주문 1건뿐


def test_reprice_across_attempts_all_maker():
    """호가가 도망가도 매 회차 새 BBO 로 다시 붙어 전량 maker 로 채운다(taker 0)."""
    fake = FakeCCXT(books=[(100.0, 101.0), (100.5, 101.5)], limit_fills=[0.4, 0.6])
    fill = _Broker(fake).limit_then_market("buy", 1.0, TIMEOUT, max_attempts=5)
    assert _near(fill.qty, 1.0) and _near(fill.taker_qty, 0.0)
    assert _near(fill.maker_qty, 1.0)
    assert _near(fill.price, (0.4 * 100.0 + 0.6 * 100.5) / 1.0)   # 회차별 재호가 평단
    assert [c[0] for c in fake.created] == ["limit", "limit"]     # 재호가 2회, 시장가 없음
    assert fake.created[1][2] == 0.6                             # 2회차는 '남은 수량'만


def test_all_attempts_miss_then_full_taker():
    """전 회차 미체결이면 소진 후 남은 전량을 시장가로 마무리한다."""
    fake = FakeCCXT(books=(100.0, 101.0), limit_fills=[0.0, 0.0, 0.0], market_px=101.0)
    fill = _Broker(fake).limit_then_market("buy", 1.0, TIMEOUT, max_attempts=3)
    assert _near(fill.qty, 1.0) and _near(fill.maker_qty, 0.0) and _near(fill.taker_qty, 1.0)
    assert _near(fill.price, 101.0)
    assert [c[0] for c in fake.created] == ["limit", "limit", "limit", "market"]


def test_partial_maker_then_taker_remainder():
    """회차마다 조금씩 maker 로 먹고, 소진 후 남은 수량만 taker — 이미 먹은 maker 는 유지."""
    fake = FakeCCXT(books=(100.0, 101.0), limit_fills=[0.3, 0.2], market_px=101.0)
    fill = _Broker(fake).limit_then_market("buy", 1.0, TIMEOUT, max_attempts=2)
    assert _near(fill.qty, 1.0)
    assert _near(fill.maker_qty, 0.5) and _near(fill.taker_qty, 0.5)
    assert _near(fill.price, (0.3 * 100.0 + 0.2 * 100.0 + 0.5 * 101.0) / 1.0)
    assert [c[0] for c in fake.created] == ["limit", "limit", "market"]


def test_gtx_reject_retries_instead_of_giving_up():
    """★ 교차 거부(-5022)는 **일시적 레이스**다 — 여기서 추격을 포기하면 안 된다.

    예전엔 break 로 남은 전량을 시장가에 밀었다. 실측(2026-09-02)이 이게 maker 비율을 2.5% 로
    만든 진짜 원인임을 보여줬다: 5회 설정인데 지정가가 평균 1.2회만 걸리고 끝났다(fill_log 의
    orders 역산). **대기 시간을 늘려도 소용없다 — 거부는 대기 '전'에 나기 때문이다.**
    """
    fake = FakeCCXT(books=(100.0, 101.0), limit_fills=[0.0, 1.0], market_px=101.0, reject_at={0})
    fill = _Broker(fake).limit_then_market("buy", 1.0, TIMEOUT, max_attempts=3)
    assert _near(fill.maker_qty, 1.0) and _near(fill.taker_qty, 0.0)
    assert [c[0] for c in fake.created] == ["limit"]      # 거부는 기록 안 됨. 2회차가 전량 maker


def test_gtx_reject_every_attempt_still_ends_in_taker():
    """전 회차가 거부되면 결국 시장가 — 재시도가 무한루프가 되면 안 된다."""
    fake = FakeCCXT(books=(100.0, 101.0), limit_fills=[], market_px=101.0, reject_at={0, 1, 2})
    fill = _Broker(fake).limit_then_market("buy", 1.0, TIMEOUT, max_attempts=3)
    assert _near(fill.taker_qty, 1.0)
    assert [c[0] for c in fake.created] == ["market"]
    assert fake._limit_i == 3                            # 회차는 다 써봤다


class _TickBroker(_Broker):
    """틱 크기를 아는 브로커 — BBO 에서 물러나는 동작 검증용."""

    def market(self):
        m = dict(super().market())
        m["info"] = {"filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.10"}]}
        return m


def test_passive_tick_places_behind_the_bbo():
    """★ BBO 에 딱 붙이면 도달 직전에 반대 호가가 오는 순간 교차로 거부된다.

    한 틱 물러나면 교차가 **구조적으로** 불가능하다. 체결 확률은 떨어지지만 안 되면 어차피
    시장가라 하한은 같다 — 그래서 기다릴 수 있는 진입 쪽에서는 명백히 이득이다.
    """
    fake = FakeCCXT(books=(100.0, 101.0), limit_fills=[1.0])
    b = _TickBroker(fake)
    assert _near(b.price_tick(), 0.1)
    b.limit_then_market("buy", 1.0, TIMEOUT, max_attempts=3, passive_ticks=1)
    assert _near(fake.created[0][3], 99.9)               # bid 100.0 - 1틱

    fake2 = FakeCCXT(books=(100.0, 101.0), limit_fills=[1.0])
    _TickBroker(fake2).limit_then_market("sell", 1.0, TIMEOUT, max_attempts=3, passive_ticks=1)
    assert _near(fake2.created[0][3], 101.1)             # ask 101.0 + 1틱


def test_passive_ticks_zero_keeps_old_bbo_behaviour():
    """0 이면 예전처럼 BBO 에 딱 붙인다 — 되돌릴 수 있는 스위치로 남긴다."""
    fake = FakeCCXT(books=(100.0, 101.0), limit_fills=[1.0])
    _TickBroker(fake).limit_then_market("buy", 1.0, TIMEOUT, max_attempts=3, passive_ticks=0)
    assert _near(fake.created[0][3], 100.0)


def test_passive_ticks_defaults_to_one():
    """기본이 0 이면 -5022 를 다시 맞는다 — 기본값 자체를 잠근다."""
    assert BinanceBroker.DEFAULT_PASSIVE_TICKS == 1
    assert _Broker(FakeCCXT(books=(100.0, 101.0), limit_fills=[1.0])).passive_ticks == 1


def test_sell_side_attaches_to_ask():
    """매도는 best ask 에 붙어야 maker(반대쪽에 걸면 즉시 체결돼 taker)."""
    fake = FakeCCXT(books=(100.0, 101.0), limit_fills=[1.0])
    fill = _Broker(fake).limit_then_market("sell", 1.0, TIMEOUT, max_attempts=5)
    assert _near(fill.price, 101.0) and _near(fill.maker_qty, 1.0)


def test_reduce_only_markets_dust_remainder():
    """청산(reduce_only)이면 최소주문 미만 잔량(dust)도 시장가로 마무리 → 포지션이 안 갇힌다."""
    fake = FakeCCXT(books=(100.0, 101.0), limit_fills=[0.001], market_px=101.0)
    fill = _Broker(fake).limit_then_market("sell", 0.0015, TIMEOUT, reduce_only=True, max_attempts=1)
    assert _near(fill.qty, 0.0015)                       # 0.001 maker + 0.0005 dust taker = 전량
    assert [c[0] for c in fake.created] == ["limit", "market"]   # dust 도 시장가로 나감


def test_entry_keeps_min_guard_on_dust():
    """진입(reduce_only=False)이면 최소주문 미만 잔량은 굳이 사지 않고 그대로 둔다(기존 동작)."""
    fake = FakeCCXT(books=(100.0, 101.0), limit_fills=[0.001], market_px=101.0)
    fill = _Broker(fake).limit_then_market("buy", 0.0015, TIMEOUT, reduce_only=False, max_attempts=1)
    assert _near(fill.qty, 0.001)                        # dust 는 스킵
    assert [c[0] for c in fake.created] == ["limit"]     # 시장가 없음


def test_attempts_one_is_legacy_behavior():
    """max_attempts=1 이면 '한 번 걸고 안 되면 taker'(구 동작)와 동일해야 한다."""
    fake = FakeCCXT(books=(100.0, 101.0), limit_fills=[0.0], market_px=101.0)
    fill = _Broker(fake).limit_then_market("buy", 1.0, TIMEOUT, max_attempts=1)
    assert _near(fill.taker_qty, 1.0)
    assert [c[0] for c in fake.created] == ["limit", "market"]   # 재호가 없음


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
