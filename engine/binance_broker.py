"""바이낸스 USDⓈ-M 선물 주문 실행 — ccxt 얇은 래퍼.

'거래소 쪽 사정'만 여기 모은다: 심볼/정밀도 해석, BBO 조회, post-only→시장가 폴백 체결,
포지션·잔고·펀딩 조회. Executor 의미론(포지션 상태·손익·원장)은 `executor.py` 에 남는다.

왜 갈라두나: 실주문 경로는 테스트가 제일 어려운 부분인데, 이렇게 나누면
`tests/test_live_executor.py` 가 **가짜 브로커**를 끼워 LiveExecutor 의 모든 분기
(체결가 반영·최소주문 거부·강제청산·maker 폴백)를 네트워크 없이 밟을 수 있다.

체결 정책(README '다음 할 일' 의 BBO→3초→taker):
  ① post-only 지정가를 BBO(붙는 쪽)에 건다 → ② timeout 초 안에 안 채워지면 취소 →
  ③ 남은 수량을 시장가로 즉시 체결. 대부분 maker 로 빠지고, 급반전 때만 taker 비용을 낸다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


class OrderError(RuntimeError):
    """주문 불가/거부 — 진입은 건너뛰고 다음 봉에 재시도해도 되는 종류(잔고부족·최소주문 미달 등)."""


@dataclass
class Fill:
    """한 번의 '주문 의도'가 실제로 체결된 결과(여러 주문에 걸쳐 있을 수 있다)."""
    price: float                       # 수량가중 평균 체결가
    qty: float                         # 실제 체결 수량
    maker_qty: float = 0.0
    taker_qty: float = 0.0
    fee: float | None = None           # 견적통화 기준 실수수료. 확정 못 하면 None → 호출부가 공식으로 근사
    order_ids: list = field(default_factory=list)
    ts: int = 0

    @property
    def is_maker(self) -> bool:
        return self.taker_qty <= 0


class BinanceBroker:
    """ccxt binanceusdm 배선. 한 심볼 전용(트레이더가 심볼을 바꾸면 새로 만든다)."""

    def __init__(self, api_key: str, api_secret: str, testnet: bool, symbol: str,
                 poll_interval: float = 0.4):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.raw_symbol = symbol                 # 프리셋 표기(BTCUSDT)
        self.poll_interval = poll_interval
        self._ex = None
        self._symbol = None                      # ccxt 통일 심볼(BTC/USDT:USDT)
        self._market = None
        self._leverage_set = None

    # ---- 연결/메타 ----
    def client(self):
        """ccxt 클라이언트(지연 생성). ccxt 는 실거래 전용 선택 의존성."""
        if self._ex is None:
            try:
                import ccxt
            except ImportError:
                raise RuntimeError("ccxt 미설치 — pip install ccxt (실거래 전용).")
            self._ex = ccxt.binanceusdm({
                "apiKey": self.api_key, "secret": self.api_secret,
                "enableRateLimit": True,
                # fetchCurrencies=False: load_markets() 가 기본으로 스팟 SAPI(입출금 메타)를 부른다.
                # 선물 전용 키엔 그 권한이 없어 -2015 로 죽는다. 선물 매매엔 불필요한 정보.
                "options": {"defaultType": "future", "fetchCurrencies": False}})
            if self.testnet:
                self._ex.set_sandbox_mode(True)
        return self._ex

    def market(self) -> dict:
        """이 심볼의 ccxt 마켓 정보(정밀도·최소주문). 최초 1회 load_markets."""
        if self._market is None:
            ex = self.client()
            ex.load_markets()
            self._symbol, self._market = self._resolve_symbol(ex, self.raw_symbol)
        return self._market

    @property
    def symbol(self) -> str:
        self.market()
        return self._symbol

    @staticmethod
    def _resolve_symbol(ex, raw: str):
        """'BTCUSDT' → ccxt 통일 심볼. 거래소 id 로도, 통일 심볼로도 찾아본다."""
        if raw in ex.markets:
            return raw, ex.markets[raw]
        key = raw.upper().replace("/", "").split(":")[0]
        cand = ex.markets_by_id.get(key)
        if isinstance(cand, list):
            for m in cand:                       # 같은 id 로 여러 타입이 올 수 있다 → 선물(linear swap)만
                if m.get("swap") and m.get("linear"):
                    return m["symbol"], m
            if cand:
                return cand[0]["symbol"], cand[0]
        elif cand:
            return cand["symbol"], cand
        raise OrderError(f"거래소에 없는 심볼: {raw}")

    def round_qty(self, qty: float) -> float:
        """수량을 스텝사이즈로 절사(내림). 올림하면 증거금 초과로 거부될 수 있다.

        ccxt 는 스텝사이즈보다 작은 수량이면 0 을 주는 대신 InvalidOrder 를 던진다 —
        그건 '주문 못 낼 정도로 작다'는 뜻이므로 OrderError(진입 스킵)로 번역한다.
        """
        ex, m = self.client(), self.market()
        try:
            return float(ex.amount_to_precision(m["symbol"], qty))
        except Exception as e:
            raise OrderError(f"수량이 스텝사이즈 미만: {qty} ({e})")

    def round_price(self, price: float) -> float:
        ex, m = self.client(), self.market()
        try:
            return float(ex.price_to_precision(m["symbol"], price))
        except Exception as e:
            raise OrderError(f"가격 정밀도 변환 실패: {price} ({e})")

    def check_order_size(self, qty: float, price: float) -> None:
        """최소 수량/최소 명목가 검사. 미달이면 OrderError(진입 스킵)."""
        lim = (self.market().get("limits") or {})
        min_qty = ((lim.get("amount") or {}).get("min"))
        min_cost = ((lim.get("cost") or {}).get("min"))
        if qty <= 0:
            raise OrderError("수량이 0 — 스텝사이즈 절사로 사라짐(자본/레버리지 대비 심볼이 큼).")
        if min_qty and qty < float(min_qty):
            raise OrderError(f"최소 수량 미달: {qty} < {min_qty}")
        if min_cost and qty * price < float(min_cost):
            raise OrderError(f"최소 명목가 미달: {qty * price:.2f} < {min_cost}")

    # ---- 계정 상태 ----
    def equity(self, asset: str) -> float:
        bal = self.client().fetch_balance()
        return float((bal.get(asset) or {}).get("total") or 0.0)

    def position(self):
        """현재 포지션 → dict 또는 None(플랫). 재시작 동기화·강제청산 확인용."""
        ex = self.client()
        for p in ex.fetch_positions([self.symbol]):
            qty = abs(float(p.get("contracts") or 0))
            if qty <= 0:
                continue
            return {
                "side": 1 if (p.get("side") == "long") else -1,
                "qty": qty,
                "entry_price": float(p.get("entryPrice") or 0),
                "leverage": int(float(p.get("leverage") or 1)),
                "liq_price": float(p.get("liquidationPrice") or 0) or float("nan"),
                "margin_mode": p.get("marginMode"),
                "margin": float(p.get("initialMargin") or 0),
            }
        return None

    def set_leverage(self, leverage: int) -> None:
        """레버리지 설정(무포지션일 때만 의미 있음). 같은 값이면 호출 생략."""
        if self._leverage_set == leverage:
            return
        self.client().set_leverage(int(leverage), self.symbol)
        self._leverage_set = int(leverage)

    def ensure_isolated(self) -> str:
        """마진 모드를 격리로. 엔진의 청산가 공식이 격리 기준이라 크로스면 청산 판정이 어긋난다.

        이미 격리면 바이낸스가 -4046 을 주는데 그건 정상 — 삼킨다.
        """
        try:
            self.client().set_margin_mode("isolated", self.symbol)
            return "isolated"
        except Exception as e:
            if "4046" in str(e) or "No need to change" in str(e):
                return "isolated"
            return f"실패({e})"

    def position_mode(self):
        """True=헤지 모드, False=원웨이, None=확인 실패. 헤지 모드면 reduceOnly 의미가 달라진다."""
        try:
            r = self.client().fapiPrivateGetPositionSideDual()
            v = r.get("dualSidePosition")
            return v if isinstance(v, bool) else str(v).lower() == "true"
        except Exception:
            return None

    def funding_paid(self, since_ms: int, until_ms: int) -> float:
        """구간 펀딩 합계(계정 관점: 음수=지불). 실패하면 0 — 펀딩 조회 실패가 매매를 막으면 안 된다."""
        try:
            rows = self.client().fetch_funding_history(self.symbol, since=int(since_ms) - 1000)
        except Exception:
            return 0.0
        total = 0.0
        for r in rows:
            ts = int(r.get("timestamp") or 0)
            if since_ms <= ts <= until_ms:
                total += float(r.get("amount") or 0)
        return total

    def bbo(self):
        """최우선 호가 (bid, ask). post-only 지정가를 '붙이는' 기준."""
        ob = self.client().fetch_order_book(self.symbol, limit=5)
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        if not bids or not asks:
            raise OrderError("호가창이 비어 있음 — 주문 보류")
        return float(bids[0][0]), float(asks[0][0])

    # ---- 체결 ----
    def market_order(self, side: str, qty: float, reduce_only: bool = False) -> Fill:
        """시장가(taker) 즉시 체결."""
        params = {"reduceOnly": True} if reduce_only else {}
        try:
            o = self.client().create_order(self.symbol, "market", side, qty, None, params)
        except Exception as e:
            raise OrderError(f"시장가 주문 거부: {e}")
        return self._fill_of(self._settled(o), fallback_maker=False)

    def limit_then_market(self, side: str, qty: float, timeout_s: float,
                          reduce_only: bool = False) -> Fill:
        """post-only 지정가(BBO) → timeout 초 미체결분은 시장가 추격. 반환: 합산 Fill."""
        if timeout_s <= 0:
            return self.market_order(side, qty, reduce_only)
        bid, ask = self.bbo()
        limit = self.round_price(bid if side == "buy" else ask)   # 붙는 쪽에 걸어야 maker
        params = {"timeInForce": "GTX"}                           # GTX = post-only(테이커면 거부)
        if reduce_only:
            params["reduceOnly"] = True
        try:
            o = self.client().create_order(self.symbol, "limit", side, qty, limit, params)
        except Exception as e:
            if not _is_post_only_reject(e):
                raise OrderError(f"지정가 주문 거부: {e}")
            return self.market_order(side, qty, reduce_only)      # 이미 교차 → 바로 taker

        o = self._wait_fill(o, timeout_s)
        maker = self._fill_of(o, fallback_maker=True)
        remaining = self.round_qty(max(0.0, qty - maker.qty))
        if remaining <= 0:
            return maker
        try:
            self.client().cancel_order(o["id"], self.symbol)
        except Exception:
            pass                                                  # 그 사이 체결됐을 수 있다 → 아래서 재확인
        done = self._fill_of(self._settled({"id": o["id"]}), fallback_maker=True)
        remaining = self.round_qty(max(0.0, qty - done.qty))
        if remaining <= 0:
            return done
        try:
            self.check_order_size(remaining, limit)
        except OrderError:
            return done                                           # 잔량이 최소주문 미만이면 그대로 둔다
        return _merge(done, self.market_order(side, remaining, reduce_only))

    def _wait_fill(self, order: dict, timeout_s: float) -> dict:
        """지정가가 다 채워질 때까지(또는 timeout) 폴링."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if (order.get("status") in ("closed", "canceled", "rejected")):
                return order
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.time())))
            try:
                order = self.client().fetch_order(order["id"], self.symbol)
            except Exception:
                break
        return order

    def _settled(self, order: dict) -> dict:
        """체결 확정 상태로 다시 읽기(시장가도 응답이 비어 오는 경우가 있다)."""
        for _ in range(5):
            try:
                o = self.client().fetch_order(order["id"], self.symbol)
            except Exception:
                time.sleep(self.poll_interval)
                continue
            if o.get("status") in ("closed", "canceled", "rejected") or float(o.get("filled") or 0) > 0:
                return o
            time.sleep(self.poll_interval)
        return order

    def _fill_of(self, order: dict, fallback_maker: bool) -> Fill:
        """주문 → Fill. 수수료·maker여부는 체결내역(my_trades)에서 확정, 실패 시 근사."""
        filled = float(order.get("filled") or 0)
        price = float(order.get("average") or order.get("price") or 0)
        oid = order.get("id")
        fill = Fill(price=price, qty=filled, order_ids=[oid] if oid else [],
                    ts=int(order.get("timestamp") or time.time() * 1000))
        if filled <= 0:
            fill.price = 0.0
            return fill
        maker_q = taker_q = 0.0
        fee_sum, fee_ok = 0.0, True
        quote = self.market().get("quote")
        trades = self._trades_of(oid)
        for t in trades:
            q = float(t.get("amount") or 0)
            if t.get("takerOrMaker") == "maker":
                maker_q += q
            else:
                taker_q += q
            f = t.get("fee") or {}
            if f.get("currency") == quote and f.get("cost") is not None:
                fee_sum += float(f["cost"])
            else:
                fee_ok = False                    # BNB 수수료 등 → 공식 근사로 폴백
        if trades:
            if not fill.price:                    # 응답에 평단이 없으면 체결내역으로 계산
                notional = sum(float(t.get("amount") or 0) * float(t.get("price") or 0) for t in trades)
                amt = sum(float(t.get("amount") or 0) for t in trades)
                fill.price = notional / amt if amt else 0.0
            fill.fee = fee_sum if fee_ok else None
        if maker_q + taker_q <= 0:                # 체결내역 조회 실패 → 주문 타입으로 추정
            maker_q, taker_q = (filled, 0.0) if fallback_maker else (0.0, filled)
        fill.maker_qty, fill.taker_qty = maker_q, taker_q
        return fill

    def _trades_of(self, order_id) -> list:
        """이 주문의 체결내역. 반영이 늦을 수 있어 몇 번 재시도."""
        if not order_id:
            return []
        for _ in range(3):
            try:
                rows = self.client().fetch_my_trades(self.symbol, limit=50)
            except Exception:
                return []
            mine = [t for t in rows if str(t.get("order")) == str(order_id)]
            if mine:
                return mine
            time.sleep(self.poll_interval)
        return []


def _is_post_only_reject(e) -> bool:
    s = str(e)
    return "-5022" in s or "post only" in s.lower() or "postonly" in s.lower()


def _merge(a: Fill, b: Fill) -> Fill:
    """두 체결 합산 — 수량가중 평단, 수수료 합(하나라도 미확정이면 미확정)."""
    qty = a.qty + b.qty
    if qty <= 0:
        return a
    fee = None if (a.fee is None or b.fee is None) else a.fee + b.fee
    return Fill(price=(a.price * a.qty + b.price * b.qty) / qty, qty=qty,
                maker_qty=a.maker_qty + b.maker_qty, taker_qty=a.taker_qty + b.taker_qty,
                fee=fee, order_ids=a.order_ids + b.order_ids, ts=max(a.ts, b.ts))
