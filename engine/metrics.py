"""백테스트 성과지표.

프리셋에 항상 붙어다니는 지표(docs DESIGN.md §3):
총수익률 / MDD / 승률 / 손익비 / 샤프 / 청산횟수 / 펀딩비 누적.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np


@dataclass
class Trade:
    side: int            # +1 롱 / -1 숏
    entry_time: int
    entry_price: float
    exit_time: int
    exit_price: float
    qty: float
    leverage: int
    pnl: float           # 수수료·펀딩 포함 순손익 (계정 통화)
    fees: float
    funding: float
    exit_reason: str     # take_profit / stop_loss / trailing / signal / time / liquidation


@dataclass
class Metrics:
    initial_equity: float
    final_equity: float
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)  # (time_ms, equity)

    @property
    def total_return_pct(self) -> float:
        return (self.final_equity / self.initial_equity - 1) * 100

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self):
        return [t for t in self.trades if t.pnl > 0]

    @property
    def losses(self):
        return [t for t in self.trades if t.pnl <= 0]

    @property
    def win_rate_pct(self) -> float:
        return 100 * len(self.wins) / self.num_trades if self.num_trades else 0.0

    @property
    def profit_factor(self) -> float:
        gain = sum(t.pnl for t in self.wins)
        loss = -sum(t.pnl for t in self.losses)
        return gain / loss if loss > 0 else float("inf")

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        eq = np.array([e for _, e in self.equity_curve])
        peak = np.maximum.accumulate(eq)
        dd = (eq - peak) / peak
        return abs(dd.min()) * 100

    @property
    def num_liquidations(self) -> int:
        return sum(1 for t in self.trades if t.exit_reason == "liquidation")

    @property
    def total_funding(self) -> float:
        return sum(t.funding for t in self.trades)

    @property
    def total_fees(self) -> float:
        return sum(t.fees for t in self.trades)

    def sharpe(self, periods_per_year: float = 365) -> float:
        """트레이드별 수익률 기반 단순 샤프 (무위험이자 0 가정)."""
        if self.num_trades < 2:
            return 0.0
        rets = np.array([t.pnl for t in self.trades]) / self.initial_equity
        sd = rets.std()
        return float(rets.mean() / sd * np.sqrt(periods_per_year)) if sd > 0 else 0.0

    def summary(self) -> str:
        return (
            f"총수익률   : {self.total_return_pct:+.2f}%  "
            f"({self.initial_equity:.0f} → {self.final_equity:.0f})\n"
            f"트레이드   : {self.num_trades}건  (승 {len(self.wins)} / 패 {len(self.losses)})\n"
            f"승률       : {self.win_rate_pct:.1f}%\n"
            f"손익비(PF) : {self.profit_factor:.2f}\n"
            f"MDD        : {self.max_drawdown_pct:.2f}%\n"
            f"샤프       : {self.sharpe():.2f}\n"
            f"청산 횟수  : {self.num_liquidations}건  ⚠️\n"
            f"펀딩비 누적: {self.total_funding:+.2f}\n"
            f"수수료 누적: {self.total_fees:.2f}"
        )
