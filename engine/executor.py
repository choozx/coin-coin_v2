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
from dataclasses import dataclass, field

from . import binance_math as bm


@dataclass
class ClosedTrade:
    side: int
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    qty: float
    leverage: int
    pnl: float
    fees: float
    funding: float
    reason: str


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
            pnl=pnl, fees=fees, funding=pos.funding_accum, reason=reason)
        self.trades.append(trade)
        self.position = None
        return trade

    def accrue_funding(self, mark_price: float, funding_rate: float) -> None:
        if self.position is None:
            return
        # funding_accum 에만 쌓고 잔고 반영은 청산 시 pnl로 한 번에(이중차감 방지).
        self.position.funding_accum += bm.funding_fee(
            mark_price, self.position.qty, self.position.side, funding_rate)


class LiveExecutor(Executor):
    """바이낸스 USDⓈ-M 선물 실거래(ccxt) — 배선 골격.

    지금 구현된 것(안전): env에서 키 로드 + ccxt 연결 준비 + 읽기전용 잔고 조회.
    아직 미구현(Tier B, 테스트넷): 실주문 open/close — create_order·set_leverage·
      post-only 지정가→3초 미체결 시 taker·체결가 반영. (호출 시 NotImplementedError)

    보안: 키는 .env(gitignore)/시크릿에만. 출금권한 OFF·IP 화이트리스트 필수.
    BINANCE_TESTNET=1(기본)이면 테스트넷(가짜돈), 0이면 메인넷.
    """

    def __init__(self, testnet: bool = None):
        self.api_key = os.environ.get("BINANCE_API_KEY")
        self.api_secret = os.environ.get("BINANCE_API_SECRET")
        self.testnet = (os.environ.get("BINANCE_TESTNET", "1") == "1") if testnet is None else testnet
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
                "enableRateLimit": True, "options": {"defaultType": "future"}})
            if self.testnet:
                self._ex.set_sandbox_mode(True)      # 테스트넷 엔드포인트
        return self._ex

    def equity(self) -> float:
        """실계좌 USDT 잔고(사이징용). 읽기 전용 — 주문 안 나감."""
        bal = self._client().fetch_balance()
        return float(bal.get("USDT", {}).get("total") or 0.0)

    def open(self, pos) -> None:
        raise NotImplementedError(
            "실주문(진입) 미구현 — Tier B(테스트넷). ccxt create_order + set_leverage, "
            "post-only 지정가→3초 미체결 시 taker, 체결가로 entry_price/qty/fee 갱신 예정.")

    def close(self, exit_price: float, reason: str, exit_time: int, is_maker: bool = False):
        raise NotImplementedError("실주문(청산) 미구현 — Tier B(테스트넷).")

    def accrue_funding(self, mark_price: float, funding_rate: float) -> None:
        pass                             # 실계좌는 거래소가 펀딩 정산 → 잔고에 이미 반영(추후 동기화)
