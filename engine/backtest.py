"""백테스트 엔진 코어.

마스터 클럭 = 1분봉. 신호 판정은 상위 TF(리샘플)에서, 청산/손절 터치는
1분봉 해상도에서. 한 1분봉 안 이벤트 처리 순서(docs/binance-formulas.md §5):
  1) 펀딩 정산  2) 청산  3) 손절/익절/트레일링  4) (신호봉 종료 시) 진입/청산 신호

v1 제약:
- 동시 포지션 1개 (sizing.maxConcurrentPositions>1 미지원)
- direction: long/both→진입신호는 롱, short→진입신호는 숏. 한 트리로 롱·숏 동시는 v2.
- 펀딩비율은 상수 근사(설정값). 실제 히스토리 주입은 추후.
- MMR/cum은 단일 tier 근사.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from . import indicators as ind
from . import binance_math as bm
from .candles import Candles, resample, signal_close_index, TIMEFRAME_MINUTES
from .conditions import SeriesResolver, evaluate
from .metrics import Metrics, Trade
from .preset import Preset


@dataclass
class BacktestConfig:
    initial_equity: float = 10_000.0
    funding_rate: float = 0.0001      # 상수 근사 (0.01%/8h)
    taker_fee: float = bm.DEFAULT_TAKER_FEE
    maker_fee: float = bm.DEFAULT_MAKER_FEE
    bracket: bm.MarginBracket = None
    max_leverage: int = 125           # 계정 정책 상한 (가드레일)

    def __post_init__(self):
        if self.bracket is None:
            self.bracket = bm.MarginBracket()


@dataclass
class _Position:
    side: int
    entry_time: int
    entry_price: float
    qty: float
    leverage: int
    margin: float
    liq_price: float
    stop_price: float      # nan 가능
    tp_price: float        # nan 가능
    entry_fee: float
    entry_signal_idx: int
    peak: float            # 트레일링용 최고가(롱)/최저가(숏)
    funding_accum: float = 0.0


def _size_position(sizing: dict, equity: float, price: float, leverage: int, stop_price: float):
    """sizing 규칙 → (qty, margin). 불가하면 (None, None).

    riskPercent는 손절가(stop_price)까지의 거리로 수량을 역산한다.
    """
    s = sizing["size"]
    if s["type"] == "equityPercent":
        margin = equity * s["value"] / 100.0
        qty = margin * leverage / price
        return qty, margin
    if s["type"] == "fixedQuote":
        notional = s["value"]
        return notional / price, notional / leverage
    if s["type"] == "fixedBase":
        qty = s["value"]
        return qty, qty * price / leverage
    if s["type"] == "riskPercent":
        if stop_price is None or np.isnan(stop_price):
            return None, None                          # 손절 없으면 리스크 사이징 불가
        dist = abs(price - stop_price)
        if dist <= 0:
            return None, None
        risk_amount = equity * s["value"] / 100.0
        qty = risk_amount / dist                       # 손절 맞으면 정확히 risk_amount 손실
        return qty, qty * price / leverage
    return None, None


def _exit_level(cfg: dict, entry: float, side: int, atr_val: float, signal, sb: int):
    """익절/손절 목표가. cfg 없으면 nan.

    side: percent/atrMultiple의 방향 부호(롱 익절은 위, 손절은 아래). 호출부에서 조정.
    swing 타입은 side 무관 — 전저(low)/전고(high) 자체를 사용.
    """
    if cfg is None:
        return float("nan")
    t = cfg["type"]
    if t == "percent":
        return entry * (1 + side * cfg["value"] / 100.0)
    if t == "atrMultiple":
        if np.isnan(atr_val):
            return float("nan")
        return entry + side * cfg["value"] * atr_val
    if t == "price":
        return cfg["value"]
    if t in ("swingLow", "swingHigh"):
        lb = int(cfg["lookback"])
        buf = cfg.get("bufferPercent", 0.0) / 100.0
        s = max(0, sb - lb + 1)
        if t == "swingLow":
            return signal.low[s:sb + 1].min() * (1 - buf)     # 전저점 아래
        return signal.high[s:sb + 1].max() * (1 + buf)        # 전고점 위
    return float("nan")


def _minutes_to_next_funding(open_time_ms: int) -> int:
    dt = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
    for h in (0, 8, 16, 24):
        boundary = dt.replace(hour=h % 24, minute=0, second=0, microsecond=0)
        if h == 24:
            boundary = boundary.fromtimestamp(boundary.timestamp() + 24 * 3600, tz=timezone.utc)
        if boundary >= dt:
            return int((boundary.timestamp() - dt.timestamp()) / 60)
    return 999


def _in_trading_hours(open_time_ms: int, windows) -> bool:
    if not windows:
        return True
    dt = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
    hm = dt.hour * 60 + dt.minute
    for w in windows:
        fh, fm = map(int, w["from"].split(":"))
        th, tm = map(int, w["to"].split(":"))
        start, end = fh * 60 + fm, th * 60 + tm
        if start <= hm < end:
            return True
    return False


def run(base: Candles, preset: Preset, cfg: BacktestConfig = None) -> Metrics:
    cfg = cfg or BacktestConfig()
    tf_min = TIMEFRAME_MINUTES[preset.timeframe]

    signal = resample(base, tf_min)
    resolver = SeriesResolver(signal)
    atr_series = ind.atr(signal.high, signal.low, signal.close, 14)
    bar_of, is_close = signal_close_index(base, tf_min)

    direction = preset.direction
    entry_side = -1 if direction == "short" else 1
    # 방향별 진입 규칙 (있으면 우선). 각 규칙 = (side, when-조건). 먼저 참인 규칙으로 진입.
    entry_rules = preset.data.get("entryRules")

    sizing = preset.sizing
    ex = preset.exit
    filt = preset.filter
    lev = min(int(sizing["leverage"]), cfg.max_leverage)

    equity = cfg.initial_equity
    pos: _Position = None
    trades = []
    equity_curve = [(int(base.open_time[0]), equity)]
    last_exit_signal_idx = -10 ** 9

    def close_position(exit_price, exit_time, reason):
        nonlocal equity, pos
        exit_fee = bm.trade_fee(exit_price, pos.qty, taker=True,
                                taker_fee=cfg.taker_fee, maker_fee=cfg.maker_fee)
        gross = pos.side * (exit_price - pos.entry_price) * pos.qty
        fees = pos.entry_fee + exit_fee
        pnl = gross - fees + pos.funding_accum
        equity += pnl
        trades.append(Trade(
            side=pos.side, entry_time=pos.entry_time, entry_price=pos.entry_price,
            exit_time=exit_time, exit_price=exit_price, qty=pos.qty, leverage=pos.leverage,
            pnl=pnl, fees=fees, funding=pos.funding_accum, exit_reason=reason,
        ))
        equity_curve.append((int(exit_time), equity))
        pos = None

    n = len(base)
    for t in range(n):
        ot = int(base.open_time[t])
        hi, lo, cl = base.high[t], base.low[t], base.close[t]

        # ---- 포지션 보유 중: 1분봉 해상도 관리 ----
        if pos is not None:
            # 1) 펀딩 정산
            if bm.is_funding_time(ot):
                f = bm.funding_fee(cl, pos.qty, pos.side, cfg.funding_rate)
                pos.funding_accum += f
                equity += f  # 현금흐름 즉시 반영

            # 2) 청산 (손절보다 먼저!)
            liquidated = (pos.side == 1 and lo <= pos.liq_price) or \
                         (pos.side == -1 and hi >= pos.liq_price)
            if liquidated:
                close_position(pos.liq_price, ot, "liquidation")

        if pos is not None:
            # 3) 손절 / 트레일링 / 익절 (보수적으로 나쁜 것부터)
            # 트레일링 피크 갱신
            trailing = ex.get("trailing")
            if pos.side == 1:
                pos.peak = max(pos.peak, hi)
                # 손절
                if not np.isnan(pos.stop_price) and lo <= pos.stop_price:
                    close_position(pos.stop_price, ot, "stop_loss")
                elif trailing and _trailing_hit(pos, trailing, lo, hi):
                    close_position(_trailing_stop(pos, trailing), ot, "trailing")
                elif not np.isnan(pos.tp_price) and hi >= pos.tp_price:
                    close_position(pos.tp_price, ot, "take_profit")
            else:
                pos.peak = min(pos.peak, lo)
                if not np.isnan(pos.stop_price) and hi >= pos.stop_price:
                    close_position(pos.stop_price, ot, "stop_loss")
                elif trailing and _trailing_hit(pos, trailing, lo, hi):
                    close_position(_trailing_stop(pos, trailing), ot, "trailing")
                elif not np.isnan(pos.tp_price) and lo <= pos.tp_price:
                    close_position(pos.tp_price, ot, "take_profit")

        # ---- 신호봉 종료 시점: 진입/청산 신호 판정 ----
        if is_close[t]:
            sb = int(bar_of[t])
            sig_close = signal.close[sb]

            # 청산 신호 (조건/시간)
            if pos is not None:
                cond = ex.get("condition")
                time_stop = ex.get("timeStop")
                if cond is not None and evaluate(cond, resolver, sb):
                    close_position(sig_close, ot, "signal")
                elif time_stop is not None:
                    bars_held = sb - pos.entry_signal_idx
                    if bars_held >= time_stop["maxBars"]:
                        close_position(sig_close, ot, "time")

            # 진입 신호
            if pos is None and _entry_allowed(sb, ot, filt, last_exit_signal_idx, cfg):
                side = None
                if entry_rules:
                    for rule in entry_rules:               # 순서대로 평가, 먼저 참인 규칙의 방향
                        if evaluate(rule["when"], resolver, sb):
                            side = 1 if rule["side"] == "long" else -1
                            break
                elif evaluate(preset.entry, resolver, sb):
                    side = entry_side
                if side is not None:
                    p = _open_position(preset, sizing, ex, sig_close, ot, sb,
                                       side, lev, equity, cfg, atr_series, signal)
                    if p is not None:
                        pos = p

        # 마킹: 무포지션 구간도 자산곡선에 점 남김(선택)
        if pos is None and is_close[t]:
            equity_curve.append((ot, equity))

    # 종료 시 잔여 포지션 청산(마지막 종가)
    if pos is not None:
        close_position(base.close[-1], int(base.open_time[-1]), "signal")

    m = Metrics(cfg.initial_equity, equity, trades, equity_curve)
    return m


def _trailing_stop(pos: _Position, trailing: dict) -> float:
    cb = trailing["callbackPercent"] / 100.0
    if pos.side == 1:
        return pos.peak * (1 - cb)
    return pos.peak * (1 + cb)


def _trailing_hit(pos: _Position, trailing: dict, lo: float, hi: float) -> bool:
    # 활성화 조건: activationPercent 수익률 도달 후
    act = trailing.get("activationPercent", 0.0) / 100.0
    if pos.side == 1:
        activated = pos.peak >= pos.entry_price * (1 + act)
        return activated and lo <= _trailing_stop(pos, trailing)
    else:
        activated = pos.peak <= pos.entry_price * (1 - act)
        return activated and hi >= _trailing_stop(pos, trailing)


def _entry_allowed(sb, ot, filt, last_exit_idx, cfg) -> bool:
    if filt.get("cooldownBars"):
        if sb - last_exit_idx < filt["cooldownBars"]:
            return False
    if filt.get("avoidFundingWindowMinutes"):
        if _minutes_to_next_funding(ot) <= filt["avoidFundingWindowMinutes"]:
            return False
    if filt.get("maxFundingRate") is not None:
        if abs(cfg.funding_rate) > filt["maxFundingRate"]:
            return False
    if not _in_trading_hours(ot, filt.get("tradingHoursUTC")):
        return False
    return True


def _open_position(preset, sizing, ex, price, ot, sb, side, lev, equity, cfg, atr_series, signal):
    atr_val = atr_series[sb]
    # 손절/익절가 먼저 계산 (리스크 사이징이 손절 거리를 필요로 함).
    # 롱 손절은 아래(-side), 익절은 위(+side). swing 타입은 side 무시.
    sl_cfg, tp_cfg = ex.get("stopLoss"), ex.get("takeProfit")
    stop_price = _exit_level(sl_cfg, price, -side, atr_val, signal, sb) if sl_cfg else float("nan")
    tp_price = _exit_level(tp_cfg, price, side, atr_val, signal, sb) if tp_cfg else float("nan")

    qty, margin = _size_position(sizing, equity, price, lev, stop_price)
    if qty is None or qty <= 0 or margin is None or margin <= 0:
        return None
    if margin > equity:                     # 리스크 사이징이 자본 초과 요구 → 자본 상한으로 캡
        margin = equity
        qty = margin * lev / price

    liq = bm.liquidation_price(price, qty, lev, side, cfg.bracket, wallet_balance=margin)

    # 청산가 안전버퍼 필터
    buf = sizing.get("minLiquidationBuffer")
    if buf is not None:
        if abs(liq - price) / price * 100 < buf:
            return None                     # 청산가가 너무 가까움 → 진입 스킵

    entry_fee = bm.trade_fee(price, qty, taker=True,
                             taker_fee=cfg.taker_fee, maker_fee=cfg.maker_fee)
    return _Position(
        side=side, entry_time=ot, entry_price=price, qty=qty, leverage=lev,
        margin=margin, liq_price=liq, stop_price=stop_price, tp_price=tp_price,
        entry_fee=entry_fee, entry_signal_idx=sb, peak=price,
    )
