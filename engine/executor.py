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
    """바이낸스 USDⓈ-M 선물 실거래(ccxt) — 뼈대만. 실제 주문/체결/잔고 연동은 TODO.

    구현 시 주의:
    - API 키는 코드/레포에 두지 말 것 (env/vault). 실거래는 별도 프로세스로.
    - open/close는 실제 주문 전송 후 '체결 결과'로 entry_price·qty·fee를 갱신해야 함.
    - maker 진입은 post-only 지정가 → 3초 미체결 시 취소 후 taker (README 청산 정책 참고).
    - 레버리지는 진입 전 set_leverage로 계정에 반영. 포지션/잔고는 API에서 동기화.
    """

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "실거래(LiveExecutor)는 아직 구현 안 됨 — ccxt 연동·시크릿·주문체결 TODO. "
            "지금은 PaperExecutor로 페이퍼 트레이딩부터.")
