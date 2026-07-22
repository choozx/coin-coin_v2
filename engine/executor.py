"""실행 어댑터 — 백테스트·페이퍼·실거래가 '같은 전략 엔진'을 쓰고 주문 실행만 갈아끼운다.

Executor 인터페이스:
- equity()             : 현재 잔고(사이징·레버리지 티어 계산용)
- open(pos)            : 포지션 진입 (pos = 엔진이 계산한 _Position)
- close(price, reason) : 포지션 청산
- position             : 현재 보유 포지션(_Position) 또는 None

구현체:
- PaperExecutor : 시뮬레이션(로컬 잔고/포지션). 실시간 시세로 페이퍼 트레이딩. 수수료는
  binance_math.trade_fee 재사용 → 백테스트와 동일한 손익 계산.
- LiveExecutor  : 바이낸스 실거래(ccxt). 뼈대만 — 실제 주문/체결 연동은 TODO.
"""
from __future__ import annotations

import os
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

    def open(self, pos) -> None:
        """엔진이 계산한 _Position으로 진입."""
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

    def open(self, pos) -> None:
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


class LiveExecutor(Executor):
    """바이낸스 USDⓈ-M 선물 실거래(ccxt) — 배선 골격.

    지금 구현된 것(안전): env에서 키 로드 + ccxt 연결 준비 + 읽기전용 잔고 조회.
    아직 미구현(Tier B, 테스트넷): 실주문 open/close — create_order·set_leverage·
      post-only 지정가→3초 미체결 시 taker·체결가 반영. (호출 시 NotImplementedError)

    보안: 키는 .env(gitignore)/시크릿에만. 출금권한 OFF·IP 화이트리스트 필수.
    BINANCE_TESTNET=1(기본)이면 테스트넷(가짜돈), 0이면 메인넷.
    """

    def __init__(self, testnet: bool = None, symbol: str = None):
        self.api_key = os.environ.get("BINANCE_API_KEY")
        self.api_secret = os.environ.get("BINANCE_API_SECRET")
        self.testnet = _testnet_flag() if testnet is None else testnet
        # 사이징 기준 자산 = 그 심볼의 마진 자산. BTCUSDC는 USDC로 증거금을 잡으므로
        # USDT 잔고로 수량을 계산하면 '있지도 않은 돈' 기준이 된다(증거금 부족 거부).
        self.quote_asset = margin_asset(symbol)
        if not self.api_key or not self.api_secret:
            raise RuntimeError(
                "실거래: BINANCE_API_KEY/BINANCE_API_SECRET 환경변수가 없습니다(.env). "
                "출금권한 OFF·IP 화이트리스트 필수. 테스트넷은 BINANCE_TESTNET=1.")
        self._ex = None                  # ccxt 클라이언트(지연 생성)
        self.position = None

    def _client(self):
        """ccxt 바이낸스 선물 클라이언트(지연 생성). ccxt는 실거래 전용 선택 의존성."""
        if self._ex is None:
            try:
                import ccxt
            except ImportError:
                raise RuntimeError("ccxt 미설치 — pip install ccxt (실거래 전용).")
            self._ex = ccxt.binanceusdm({
                "apiKey": self.api_key, "secret": self.api_secret,
                "enableRateLimit": True,
                # fetchCurrencies=False: ccxt의 load_markets()가 기본으로 스팟 SAPI
                # (/sapi/v1/capital/config/getall = 입출금 메타)를 부른다. 선물 전용 키엔
                # 그 권한이 없어 잔고 조회가 -2015로 죽는다. 선물 매매엔 불필요한 정보 —
                # tick/step size 등 마켓 정보는 fapi exchangeInfo에서 따로 온다.
                "options": {"defaultType": "future", "fetchCurrencies": False}})
            if self.testnet:
                self._ex.set_sandbox_mode(True)      # 테스트넷 엔드포인트
        return self._ex

    def equity(self) -> float:
        """실계좌의 '그 심볼 마진 자산' 잔고(사이징용). 읽기 전용 — 주문 안 나감."""
        bal = self._client().fetch_balance()
        return float(bal.get(self.quote_asset, {}).get("total") or 0.0)

    def open(self, pos) -> None:
        raise NotImplementedError(
            "실주문(진입) 미구현 — Tier B(테스트넷). ccxt create_order + set_leverage, "
            "post-only 지정가→3초 미체결 시 taker, 체결가로 entry_price/qty/fee 갱신 예정.")

    def close(self, exit_price: float, reason: str, exit_time: int, is_maker: bool = False):
        raise NotImplementedError("실주문(청산) 미구현 — Tier B(테스트넷).")

    def accrue_funding(self, mark_price: float, funding_rate: float) -> None:
        pass                             # 실계좌는 거래소가 펀딩 정산 → 잔고에 이미 반영(추후 동기화)
