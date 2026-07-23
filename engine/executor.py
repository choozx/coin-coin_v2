"""실행 어댑터 — 백테스트·페이퍼·실거래가 '같은 전략 엔진'을 쓰고 주문 실행만 갈아끼운다.

Executor 인터페이스:
- equity()             : 현재 잔고(사이징·레버리지 티어 계산용)
- open(pos)            : 포지션 진입 (pos = 엔진이 계산한 _Position)
- close(price, reason) : 포지션 청산
- position             : 현재 보유 포지션(_Position) 또는 None

구현체:
- PaperExecutor : 시뮬레이션(로컬 잔고/포지션). 실시간 시세로 페이퍼 트레이딩. 수수료는
  binance_math.trade_fee 재사용 → 백테스트와 동일한 손익 계산.
- LiveExecutor  : 바이낸스 실거래(ccxt). 주문은 binance_broker 가 내고, 여기선 '체결 결과를
  엔진 상태에 반영'만 한다 — 실제 체결가/수량/수수료로 포지션을 덮어쓰는 게 핵심.
"""
from __future__ import annotations

import json
import os
import time

from . import binance_math as bm
from .metrics import Trade


# 청산된 거래는 metrics.Trade 한 타입으로 통일한다. 예전엔 백테스트(metrics.Trade)와
# 페이퍼/실거래(자체 ClosedTrade)가 필드명까지 다른 타입(exit_reason vs reason)을 써서,
# 같은 거래를 다루는 코드가 두 갈래로 갈렸다. 이름은 호환을 위해 유지.
ClosedTrade = Trade


class Executor:
    """실행 어댑터 인터페이스. 트레이더는 이 API로만 주문한다(백테스트/페이퍼/실거래 공통)."""

    position = None          # 현재 _Position 또는 None

    def equity(self) -> float:
        raise NotImplementedError

    def open(self, pos, is_maker: bool = False) -> None:
        """엔진이 계산한 _Position으로 진입. is_maker=엔진이 가정한 진입 체결 유형."""
        raise NotImplementedError

    def close(self, exit_price: float, reason: str, exit_time: int, is_maker: bool = False):
        """현재 포지션 청산 → ClosedTrade 반환."""
        raise NotImplementedError

    def accrue_funding(self, mark_price: float, funding_rate: float) -> None:
        """보유 중 펀딩 정산(8시간마다). 기본 구현 없음(페이퍼만 사용)."""


class PaperExecutor(Executor):
    """페이퍼 트레이딩 — 로컬 잔고/포지션 시뮬레이션. 백테스트와 동일한 수수료·손익 공식."""

    def __init__(self, equity: float = 10_000.0,
                 taker_fee: float = bm.DEFAULT_TAKER_FEE, maker_fee: float = bm.DEFAULT_MAKER_FEE):
        self._equity = float(equity)
        self.taker_fee = taker_fee
        self.maker_fee = maker_fee
        self.position = None
        self.trades: list[ClosedTrade] = []

    def equity(self) -> float:
        return self._equity

    def open(self, pos, is_maker: bool = False) -> None:
        if self.position is not None:
            raise RuntimeError("이미 포지션 보유 중")
        self.position = pos          # 손익·수수료는 청산 시 한 번에 정산(백테스트와 동일)

    def close(self, exit_price: float, reason: str, exit_time: int, is_maker: bool = False):
        pos = self.position
        if pos is None:
            raise RuntimeError("청산할 포지션 없음")
        exit_fee = bm.trade_fee(exit_price, pos.qty, taker=not is_maker,
                                taker_fee=self.taker_fee, maker_fee=self.maker_fee)
        gross = pos.side * (exit_price - pos.entry_price) * pos.qty
        fees = pos.entry_fee + exit_fee
        pnl = gross - fees + pos.funding_accum      # 진입+청산 수수료, 펀딩 누적 반영
        self._equity += pnl
        trade = ClosedTrade(
            side=pos.side, entry_time=pos.entry_time, entry_price=pos.entry_price,
            exit_time=exit_time, exit_price=exit_price, qty=pos.qty, leverage=pos.leverage,
            pnl=pnl, fees=fees, funding=pos.funding_accum, exit_reason=reason,
            stop_price=pos.stop_price, tp_price=pos.tp_price)      # 차트·원장용
        self.trades.append(trade)
        self.position = None
        return trade

    def accrue_funding(self, mark_price: float, funding_rate: float) -> None:
        if self.position is None:
            return
        # funding_accum 에만 쌓고 잔고 반영은 청산 시 pnl로 한 번에(이중차감 방지).
        self.position.funding_accum += bm.funding_fee(
            mark_price, self.position.qty, self.position.side, funding_rate)


_MARGIN_ASSETS = ("USDC", "USDT", "BUSD", "FDUSD")   # USDⓈ-M 선물의 견적/마진 자산


def margin_asset(symbol: str, default: str = "USDT") -> str:
    """심볼의 마진(견적) 자산. BTCUSDC→USDC, BTCUSDT→USDT.

    바이낸스 USDⓈ-M은 자산별로 증거금이 분리돼 있다(멀티에셋 모드 OFF 기준) —
    USDC 심볼을 USDT 잔고 기준으로 사이징하면 계산과 실제 증거금이 어긋난다.
    """
    if not symbol:
        return default
    s = symbol.upper().replace("/", "").split(":")[0]
    for a in _MARGIN_ASSETS:
        if s.endswith(a):
            return a
    return default


_TESTNET_ON = {"1", "true", "yes", "on"}      # 가짜돈
_TESTNET_OFF = {"0", "false", "no", "off"}    # 실돈


def _testnet_flag() -> bool:
    """BINANCE_TESTNET 해석. 기본(미설정)은 테스트넷.

    실돈은 '명시적으로' 껐을 때만. 알아볼 수 없는 값이면 조용히 한쪽을 고르지 않고 에러 —
    오타 하나가 가짜돈/실돈을 가르므로 애매한 설정은 실행을 막는 게 맞다.
    """
    raw = os.environ.get("BINANCE_TESTNET")
    if raw is None or raw.strip() == "":
        return True
    v = raw.strip().lower()
    if v in _TESTNET_ON:
        return True
    if v in _TESTNET_OFF:
        return False
    raise RuntimeError(
        f"BINANCE_TESTNET 값을 알 수 없습니다: {raw!r} — 1(테스트넷)/0(메인넷)만 허용. "
        "(.env에 인라인 주석이 값에 붙지 않았는지 확인)")


DEFAULT_FILL_TIMEOUT = 3.0       # post-only 지정가를 기다리는 초 — 넘으면 시장가 추격
LIVE_POSITION_PATH = os.environ.get("LIVE_POSITION_PATH", "data/live_position.json")


class LiveExecutor(Executor):
    """바이낸스 USDⓈ-M 선물 실거래(ccxt).

    엔진(Stepper)은 '이 가격에 이 수량으로 들어가라'고 _Position 을 계산해 넘기지만,
    **실제 시장은 그 가격에 안 준다.** 이 클래스의 일은 주문을 내고 돌아온 진짜 체결가·수량·
    수수료로 그 _Position 을 덮어써서, 이후 엔진의 손절/청산 판정이 허구가 아닌 실제 포지션을
    기준으로 돌게 만드는 것이다. 청산가도 우리 근사식 대신 **거래소가 계산한 값**으로 바꾼다.

    체결 정책(진입·maker 청산 공통): post-only 지정가(BBO) → `fill_timeout_s` 초 미체결 시
    취소 → 시장가 추격. 손절·강제청산은 언제나 시장가(확실히 빠져나가는 게 우선).

    보안: 키는 .env(gitignore)/시크릿에만. 출금권한 OFF·IP 화이트리스트 필수.
    BINANCE_TESTNET=1(기본)이면 테스트넷(가짜돈), 0이면 메인넷.
    """

    def __init__(self, testnet: bool = None, symbol: str = None,
                 taker_fee: float = None, maker_fee: float = None,
                 fill_timeout_s: float = None, broker=None,
                 position_path: str = LIVE_POSITION_PATH):
        self.api_key = os.environ.get("BINANCE_API_KEY")
        self.api_secret = os.environ.get("BINANCE_API_SECRET")
        self.testnet = _testnet_flag() if testnet is None else testnet
        self.symbol = symbol
        # 사이징 기준 자산 = 그 심볼의 마진 자산. BTCUSDC는 USDC로 증거금을 잡으므로
        # USDT 잔고로 수량을 계산하면 '있지도 않은 돈' 기준이 된다(증거금 부족 거부).
        self.quote_asset = margin_asset(symbol)
        mk, tk = bm.fees_for_symbol(symbol or "")
        self.maker_fee = mk if maker_fee is None else maker_fee
        self.taker_fee = tk if taker_fee is None else taker_fee
        self.fill_timeout_s = (float(os.environ.get("MAKER_FILL_TIMEOUT_SEC", DEFAULT_FILL_TIMEOUT))
                               if fill_timeout_s is None else fill_timeout_s)
        self.position_path = position_path
        self.position = None
        self.trades: list[ClosedTrade] = []      # 원장에서 복원됨(가드레일의 연속손실 계산용)
        self._equity_cache = (0.0, 0.0)          # (조회시각, 값) — 폴링마다 때리지 않게 짧게 캐시
        self._own_broker = broker is None        # 우리가 만든 브로커여야 심볼 변경 시 새로 만든다
        if broker is not None:
            self._broker = broker                # 테스트용 주입
            return
        if not self.api_key or not self.api_secret:
            raise RuntimeError(
                "실거래: BINANCE_API_KEY/BINANCE_API_SECRET 환경변수가 없습니다(.env). "
                "출금권한 OFF·IP 화이트리스트 필수. 테스트넷은 BINANCE_TESTNET=1.")
        from .binance_broker import BinanceBroker
        self._broker = BinanceBroker(self.api_key, self.api_secret, self.testnet, symbol)

    @property
    def broker(self):
        return self._broker

    def set_symbol(self, symbol: str) -> None:
        """심볼 변경(대시보드 봇설정/전략 전환) — 브로커·마진자산을 새 심볼로 갈아끼운다.

        안 하면 브로커가 옛 심볼에 묶인 채로 남아 **엉뚱한 종목에 주문이 나간다.**
        무포지션일 때만 허용(트레이더도 포지션 있으면 설정 반영을 미룬다).
        """
        if not symbol or symbol == self.symbol:
            return
        if self.position is not None:
            raise RuntimeError(f"포지션 보유 중엔 심볼 변경 불가: {self.symbol} → {symbol}")
        self.symbol = symbol
        self.quote_asset = margin_asset(symbol)
        self._equity_cache = (0.0, 0.0)
        if self._own_broker:
            from .binance_broker import BinanceBroker
            self._broker = BinanceBroker(self.api_key, self.api_secret, self.testnet, symbol)

    # ---- 시작 전 점검 ----
    def preflight(self) -> dict:
        """실매매 시작 전 계정 상태 확인. 위험한 설정은 여기서 걸러 낸다.

        - 헤지 모드면 reduceOnly 청산 의미가 달라 포지션이 안 닫힐 수 있다 → 중단.
        - 마진 모드는 격리로 맞춘다(엔진 청산가 공식이 격리 기준).
        """
        b = self._broker
        m = b.market()
        hedge = b.position_mode()
        if hedge:
            raise RuntimeError(
                "계정이 헤지 모드입니다 — 이 엔진은 원웨이(One-way) 전용입니다. "
                "바이낸스 선물 설정에서 '단방향 모드'로 바꾼 뒤 다시 시작하세요.")
        margin = b.ensure_isolated()
        eq = self.equity(force=True)
        info = {"symbol": m.get("symbol"), "testnet": self.testnet, "marginMode": margin,
                "hedgeMode": hedge, "equity": eq, "asset": self.quote_asset,
                "position": b.position()}
        print(f"[실거래 점검] {info['symbol']} · {'테스트넷(가짜돈)' if self.testnet else '★메인넷(실돈)★'} "
              f"· 마진 {margin} · 잔고 {eq:.2f} {self.quote_asset} "
              f"· 헤지모드 {'확인불가' if hedge is None else hedge}", flush=True)
        return info

    def equity(self, force: bool = False) -> float:
        """실계좌의 '그 심볼 마진 자산' 잔고(사이징용). 3초 캐시 — 폴링·상태기록이 여러 번 부른다."""
        now = time.time()
        if not force and now - self._equity_cache[0] < 3.0:
            return self._equity_cache[1]
        val = self._broker.equity(self.quote_asset)
        self._equity_cache = (now, val)
        return val

    # ---- 진입 ----
    def open(self, pos, is_maker: bool = False) -> None:
        """실주문으로 진입하고, 돌아온 **실제 체결**로 pos 를 덮어쓴다.

        수량은 스텝사이즈로 절사한다(올리면 증거금 초과 거부). 절사 후 최소주문에 못 미치면
        OrderError — 진입을 건너뛰고 다음 신호를 기다린다(포지션은 안 생긴다).
        """
        from .binance_broker import OrderError
        if self.position is not None:
            raise RuntimeError("이미 포지션 보유 중")
        b = self._broker
        qty = b.round_qty(pos.qty)
        b.check_order_size(qty, pos.entry_price)          # 미달이면 OrderError → 진입 스킵
        b.set_leverage(pos.leverage)
        side = "buy" if pos.side == 1 else "sell"
        fill = (b.limit_then_market(side, qty, self.fill_timeout_s) if is_maker
                else b.market_order(side, qty))
        if fill.qty <= 0:
            raise OrderError("진입 주문이 하나도 안 채워짐 — 이번 신호는 건너뜀")
        # ★ 여기서부터는 실제 포지션이 존재한다. 예외로 빠져나가면 '관리되지 않는 포지션'이
        #   되므로, 상태 반영을 먼저 끝내고 부가정보(청산가·사이드카)는 나중에 채운다.
        pos.entry_price = fill.price
        pos.qty = fill.qty
        pos.entry_fee = self._fee_of(fill)
        pos.peak = fill.price
        pos.margin = fill.price * fill.qty / max(1, pos.leverage)
        self.position = pos
        self._equity_cache = (0.0, 0.0)
        self._adopt_exchange_liq(pos)
        self._save_position()

    def _adopt_exchange_liq(self, pos) -> None:
        """청산가를 거래소가 계산한 값으로 교체. 우리 근사식(단일 tier)보다 이게 진짜다."""
        try:
            p = self._broker.position()
        except Exception:
            return
        if p and p.get("liq_price") and p["liq_price"] == p["liq_price"]:   # nan 아님
            pos.liq_price = float(p["liq_price"])
        if p and p.get("margin"):
            pos.margin = float(p["margin"])

    def _fee_of(self, fill) -> float:
        """실수수료. 거래소가 알려주면 그걸, BNB 결제 등으로 못 읽으면 공식으로 근사."""
        if fill.fee is not None:
            return float(fill.fee)
        return (bm.trade_fee(fill.price, fill.maker_qty, taker=False,
                             taker_fee=self.taker_fee, maker_fee=self.maker_fee)
                + bm.trade_fee(fill.price, fill.taker_qty, taker=True,
                               taker_fee=self.taker_fee, maker_fee=self.maker_fee))

    # ---- 청산 ----
    def close(self, exit_price: float, reason: str, exit_time: int, is_maker: bool = False):
        """실주문으로 청산 → ClosedTrade. exit_price(엔진이 가정한 가격)는 참고만 하고
        실제 체결가로 기록한다. 단 강제청산은 이미 거래소가 끝낸 일이라 그 가격을 쓴다."""
        from .binance_broker import OrderError
        pos = self.position
        if pos is None:
            raise RuntimeError("청산할 포지션 없음")

        if reason == "liquidation":
            # 엔진의 청산 판정은 '우리 추정 청산가' 기준이라 거래소보다 이르거나 늦을 수 있다.
            # 실제로 털렸으면 그 사실을 기록하고, 아직 살아 있으면 시장가로 확실히 빠져나온다.
            if self._exchange_flat():
                return self._record(pos, exit_price, 0.0, reason, exit_time)
            print("  [강제청산 판정] 거래소엔 포지션이 남아 있음 → 시장가로 즉시 청산", flush=True)
            is_maker = False

        side = "sell" if pos.side == 1 else "buy"
        qty = self._broker.round_qty(pos.qty)
        fill = (self._broker.limit_then_market(side, qty, self.fill_timeout_s, reduce_only=True)
                if is_maker else self._broker.market_order(side, qty, reduce_only=True))
        if fill.qty <= 0:
            raise OrderError(f"청산 주문 미체결({reason}) — 포지션 유지, 다음 봉에 재시도")
        if fill.qty < qty * 0.999:                 # 부분청산: 남은 수량은 계속 엔진이 관리
            pos.qty = max(0.0, pos.qty - fill.qty)
            self._save_position()
            raise OrderError(
                f"부분청산({reason}): {fill.qty}/{qty} 체결, 잔량 {pos.qty} 유지 — 다음 봉에 재시도")
        return self._record(pos, fill.price, self._fee_of(fill), reason, exit_time)

    def _record(self, pos, exit_price: float, exit_fee: float, reason: str, exit_time: int):
        """청산 확정 → 손익 계산 + ClosedTrade 기록. 펀딩은 거래소 정산분을 조회해 반영."""
        funding = 0.0
        try:
            funding = self._broker.funding_paid(int(pos.entry_time), int(exit_time))
        except Exception:
            pass
        gross = pos.side * (exit_price - pos.entry_price) * pos.qty
        fees = pos.entry_fee + exit_fee
        trade = ClosedTrade(
            side=pos.side, entry_time=pos.entry_time, entry_price=pos.entry_price,
            exit_time=exit_time, exit_price=exit_price, qty=pos.qty, leverage=pos.leverage,
            pnl=gross - fees + funding, fees=fees, funding=funding, exit_reason=reason,
            stop_price=pos.stop_price, tp_price=pos.tp_price)
        self.trades.append(trade)
        self.position = None
        self._equity_cache = (0.0, 0.0)      # 잔고는 다음 조회에서 거래소 값으로(우리 계산 아님)
        self._save_position()
        return trade

    def _exchange_flat(self) -> bool:
        try:
            return self._broker.position() is None
        except Exception:
            return False                     # 확인 실패 시 '아직 있다'고 보고 시장가 청산 시도

    def accrue_funding(self, mark_price: float, funding_rate: float) -> None:
        pass                                 # 실계좌는 거래소가 정산 → 청산 시 실제 내역을 조회해 반영

    # ---- 재시작 동기화 ----
    # 봇이 죽었다 살아나도 거래소엔 포지션이 남아 있다. 로컬 상태를 맹신하지 않고 거래소를
    # 진실로 삼되, 손절/익절가처럼 거래소가 모르는 값은 사이드카 파일에서 되살린다.

    def _save_position(self) -> None:
        """현재 포지션을 사이드카에 기록(없으면 삭제). 실패해도 매매는 계속."""
        if not self.position_path:
            return
        try:
            if self.position is None:
                if os.path.exists(self.position_path):
                    os.remove(self.position_path)
                return
            p = self.position
            os.makedirs(os.path.dirname(self.position_path) or ".", exist_ok=True)
            tmp = self.position_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"symbol": self.symbol, "side": p.side, "qty": p.qty,
                           "entryPrice": p.entry_price, "entryTime": p.entry_time,
                           "leverage": p.leverage, "stop": _jnum(p.stop_price),
                           "tp": _jnum(p.tp_price), "entryFee": p.entry_fee,
                           "peak": p.peak}, f)
            os.replace(tmp, self.position_path)
        except Exception as e:
            print(f"  [포지션 사이드카 기록 실패] {e}", flush=True)

    def load_saved_position(self) -> dict:
        """사이드카 읽기(같은 심볼일 때만). 없으면 {}."""
        try:
            with open(self.position_path, encoding="utf-8") as f:
                d = json.load(f)
            return d if d.get("symbol") == self.symbol else {}
        except Exception:
            return {}

    def sync_position(self):
        """거래소의 실제 포지션. 없으면 None. (LiveTrader 가 부팅 때 호출)"""
        return self._broker.position()


def _jnum(x):
    """nan/inf 를 JSON 에 넣을 수 있게 None 으로."""
    try:
        return None if x is None or x != x else float(x)
    except TypeError:
        return None
