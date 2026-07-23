"""백테스트 엔진 코어 + **전략 판정 로직의 유일한 구현**(Stepper).

마스터 클럭 = 1분봉. 신호 판정은 상위 TF(리샘플)에서, 청산/손절 터치는
1분봉 해상도에서. 한 1분봉 안 이벤트 처리 순서(docs/binance-formulas.md §5):
  1) 펀딩 정산  2) 청산  3) 손절/익절/트레일링  4) (신호봉 종료 시) 진입/청산 신호

이 순서는 Stepper 에만 적혀 있고 백테스트·페이퍼·실거래가 그걸 그대로 호출한다
(live.py 는 폴링·핫스왑·원장 같은 라이브 고유 관심사만 담당). run() 도 PaperExecutor 를
통해 주문하므로, 세 경로가 손익·수수료 계산까지 같은 코드를 탄다.

v1 제약:
- 동시 포지션 1개 (sizing.maxConcurrentPositions>1 미지원)
- direction: long/both→진입신호는 롱, short→진입신호는 숏. 한 트리로 롱·숏 동시는 v2.
- MMR/cum은 단일 tier 근사.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from . import indicators as ind
from . import binance_math as bm
from .candles import Candles, resample, signal_close_index, TIMEFRAME_MINUTES, MINUTE_MS
from .conditions import SeriesResolver, evaluate
from .metrics import Metrics
from .preset import Preset


@dataclass
class BacktestConfig:
    initial_equity: float = 10_000.0
    funding_rate: float = 0.0001      # 상수 근사 (0.01%/8h). funding_schedule 있으면 그게 우선.
    funding_schedule: dict = None     # {funding_time_ms: rate} 실제 펀딩 히스토리. None이면 상수 근사.
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


def _leverage_for(sizing: dict, equity: float, max_lev: int) -> int:
    """진입 시점 자산(equity)에 맞는 레버리지. leverageTiers 있으면 잔고 구간 조회, 없으면 고정값.

    티어 = [{maxBalance, leverage}...] (오름차순). 현재 자산이 속한 첫 티어(equity<=maxBalance,
    maxBalance null이면 그 이상=∞)의 레버리지 사용. 모든 상한 초과 시 마지막 티어.
    """
    tiers = sizing.get("leverageTiers")
    if tiers:
        for t in tiers:
            mx = t.get("maxBalance")
            if mx is None or equity <= float(mx):
                return max(1, min(int(t["leverage"]), max_lev))
        return max(1, min(int(tiers[-1]["leverage"]), max_lev))
    return max(1, min(int(sizing["leverage"]), max_lev))


def _exit_level(cfg: dict, entry: float, side: int, atr_val: float, signal, sb: int):
    """익절/손절 목표가. cfg 없으면 nan.

    side: 진입가 기준 목표가의 방향 부호(+1 위 / -1 아래). 호출부에서 조정.
          롱 손절 -1 · 롱 익절 +1 · 숏 손절 +1 · 숏 익절 -1.
    swing: side 부호대로 전저(아래)/전고(위)를 자동 선택 → 롱 손절=전저점, 숏 손절=전고점.
    swingLow/swingHigh: 방향을 명시적으로 고정 (레거시 프리셋 호환).
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
    if t in ("swing", "swingLow", "swingHigh"):
        lb = int(cfg["lookback"])
        buf = cfg.get("bufferPercent", 0.0) / 100.0
        s = max(0, sb - lb + 1)
        use_low = (side < 0) if t == "swing" else (t == "swingLow")
        if use_low:
            return signal.low[s:sb + 1].min() * (1 - buf)     # 전저점 아래
        return signal.high[s:sb + 1].max() * (1 + buf)        # 전고점 위
    return float("nan")


def _valid_level(level: float, entry: float, side: int) -> float:
    """목표가가 진입가 기준 의도한 방향에 있는지 검사. 아니면 nan(=해당 레벨 없음).

    안전장치: 숏에 전저점 손절을 걸면 손절가가 진입가 아래에 잡히는데, 숏 손절 검사는
    hi >= stop 이라 진입 직후 즉시 '손절' 체결되며 오히려 이익이 난다(가짜 수익).
    방향이 뒤집힌 레벨은 무효로 만들어 그런 결과가 나오지 않게 한다.
    """
    if np.isnan(level):
        return float("nan")
    return level if (level - entry) * side > 0 else float("nan")


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


class Stepper:
    """1분봉 하나를 처리하는 오케스트레이션 — 백테스트·페이퍼·실거래가 **문자 그대로** 공유한다.

    이벤트 순서(이게 전략 결과를 좌우한다):
      펀딩 → 청산 → 손절/트레일링/익절 → (신호봉 마감 시) 신호청산 → 진입

    예전엔 이 순서를 backtest.run() 루프와 LiveTrader._step() 이 각각 구현하고 주석으로만
    맞춰뒀다. 한쪽만 고쳐도 테스트는 통과하고, 차이는 실거래에서야 드러났다(대조 테스트가
    실제로 세 건을 잡아냄). 이제 판정은 여기 한 곳에만 있고, 호출자는 결과를 어떻게
    기록할지(백테스트=Metrics 집계 / 라이브=이벤트·원장)만 hook 으로 다르게 한다.

    hook:
      entry_gate() -> bool   진입 허용 여부. 라이브의 멈춤·리스크 가드레일용(백테스트는 항상 True).
      on_open(pos, lev)      진입 직후.
      on_close(trade)        청산 직후(Trade).
    """

    def __init__(self, preset: Preset, cfg: BacktestConfig, executor,
                 entry_gate=None, on_open=None, on_close=None):
        self.cfg = cfg
        self.ex = executor
        self.entry_gate = entry_gate
        self.on_open = on_open
        self.on_close = on_close
        self.last_exit_sb = -10 ** 9          # 쿨다운 기준: 마지막 청산이 일어난 신호봉
        self.apply_preset(preset)

    def apply_preset(self, preset: Preset):
        """프리셋에서 파생되는 실행 파라미터 갱신(라이브의 전략 전환 시 재호출)."""
        self.preset = preset
        self.entry_rules = preset.data.get("entryRules")     # 방향별 진입 규칙(있으면 우선)
        self.entry_side = -1 if preset.direction == "short" else 1
        execution = preset.data.get("execution") or {}
        self.maker_entry = execution.get("entryType") == "makerLimit"
        # passive-then-aggressive: makerTimeoutSeconds 를 주면 post-only 지정가를 N봉 걸어두고,
        # 그 안에 가격이 지정가를 터치하면 maker 체결·아니면 마지막 봉에서 taker 추격.
        # 1분봉 해상도라 초→봉으로 반올림(최소 1봉). 미설정(0)이면 옛 동작(신호봉 종가 즉시 maker).
        sec = execution.get("makerTimeoutSeconds") or 0
        self.maker_timeout_bars = max(1, round(sec / 60)) if (self.maker_entry and sec) else 0
        self.pending = None                    # 대기 중인 지정가 진입(전략 전환/재설정 시 취소)

    def _close(self, price, reason, ts, sb, is_maker=False):
        trade = self.ex.close(price, reason, ts, is_maker=is_maker)
        self.last_exit_sb = sb                # 청산한 신호봉 → 쿨다운 기준 갱신
        if self.on_close:
            self.on_close(trade)
        return trade

    def step(self, base, signal, bar_of, is_close, atr_series, resolver, t):
        """1분봉 t 하나 처리."""
        ot = int(base.open_time[t]); hi, lo, cl = base.high[t], base.low[t], base.close[t]
        sb = int(bar_of[t])                   # 이 1분봉이 속한 신호봉
        self.manage_position(ot, hi, lo, cl, sb)
        if self.pending is not None:          # 지정가 진입 대기 중이면 이 봉에서 체결/추격 판정
            self._resolve_pending(signal, atr_series, hi, lo, cl, ot, sb)
        if is_close[t]:
            self.signal_step(signal, atr_series, resolver, ot, sb)

    def manage_position(self, ot, hi, lo, cl, sb):
        """보유 중 1분봉 해상도 관리: 펀딩 → 청산 → 손절/트레일링/익절.

        ★ 여기의 return 은 '이 메서드'만 끝낸다 — 청산이 일어난 봉에서도 호출자는 이어서
          신호 판정으로 넘어간다(청산 직후 같은 봉 재진입이 플립 전략의 전제).
        """
        cfg = self.cfg
        pos = self.ex.position
        if pos is None:
            return

        # 1) 펀딩 정산 — 잔고 반영은 청산 시 pnl로 한 번만(이중계상 방지).
        if bm.is_funding_time(ot):
            rate = cfg.funding_schedule.get(ot, cfg.funding_rate) if cfg.funding_schedule else cfg.funding_rate
            self.ex.accrue_funding(cl, rate)

        # 2) 청산 (손절보다 먼저! 레버리지 백테스트 뻥튀기 방지)
        if (pos.side == 1 and lo <= pos.liq_price) or (pos.side == -1 and hi >= pos.liq_price):
            self._close(pos.liq_price, "liquidation", ot, sb); return

        # 3) 손절 / 트레일링 / 익절 (보수적으로 나쁜 것부터)
        trailing = self.preset.exit.get("trailing")
        if pos.side == 1:
            pos.peak = max(pos.peak, hi)
            if not np.isnan(pos.stop_price) and lo <= pos.stop_price:
                self._close(pos.stop_price, "stop_loss", ot, sb)
            elif trailing and _trailing_hit(pos, trailing, lo, hi):
                self._close(_trailing_stop(pos, trailing), "trailing", ot, sb)
            elif not np.isnan(pos.tp_price) and hi >= pos.tp_price:
                self._close(pos.tp_price, "take_profit", ot, sb, is_maker=True)
        else:
            pos.peak = min(pos.peak, lo)
            if not np.isnan(pos.stop_price) and hi >= pos.stop_price:
                self._close(pos.stop_price, "stop_loss", ot, sb)
            elif trailing and _trailing_hit(pos, trailing, lo, hi):
                self._close(_trailing_stop(pos, trailing), "trailing", ot, sb)
            elif not np.isnan(pos.tp_price) and lo <= pos.tp_price:
                self._close(pos.tp_price, "take_profit", ot, sb, is_maker=True)

    def signal_step(self, signal, atr_series, resolver, ot, sb):
        """신호봉 마감 시점: 신호 기반 청산 → 진입."""
        ex, cfg, preset = self.ex, self.cfg, self.preset
        sig_close = signal.close[sb]
        # 체결 시각 = 이 1분봉이 '닫히는' 순간 = ot + 1분. (ot 는 봉의 시작)
        # 예: 5m 11:00 봉의 마지막 1분봉은 ot=11:04 이고 11:05:00 에 닫힘 → 체결은 11:05.
        # ot 를 그대로 쓰면 체결이 1분 이르게 기록돼 차트 마커가 신호봉 위에 찍힌다.
        fill_time = ot + MINUTE_MS
        ex_block = preset.exit

        # 청산 신호 (SuperTrend 전환 / 지표조건 / 시간) — elif 체인: 하나만 발동.
        pos = ex.position
        if pos is not None:
            st_exit = ex_block.get("supertrendExit")
            cond = ex_block.get("condition")
            time_stop = ex_block.get("timeStop")
            if st_exit is not None and _supertrend_flip_exit(resolver, st_exit, pos.side, sb):
                st_reason = {"stopLoss": "stop_loss", "exit": "supertrend"}.get(
                    st_exit.get("as"), "take_profit")
                # maker 모드(지정가 진입)면 SuperTrend 청산도 BBO 지정가로 체결됐다고 가정 → maker.
                # (실전: post-only 걸고 3초 내 미체결이면 취소 후 taker 청산 — README 참고.)
                self._close(sig_close, st_reason, fill_time, sb, is_maker=self.maker_entry)
            elif cond is not None and evaluate(cond, resolver, sb):
                self._close(sig_close, "signal", fill_time, sb)
            elif time_stop is not None and (sb - pos.entry_signal_idx) >= time_stop["maxBars"]:
                self._close(sig_close, "time", fill_time, sb)

        # 진입 신호. 펀딩 임박·거래시간 필터는 '체결 순간' 기준이어야 하므로 fill_time 으로 판정.
        if ex.position is not None or self.pending is not None:   # 지정가 대기 중이면 새 진입 안 냄
            return
        if self.entry_gate is not None and not self.entry_gate():
            return
        if not _entry_allowed(sb, fill_time, preset.filter, self.last_exit_sb, cfg):
            return
        side = None
        if self.entry_rules:
            for rule in self.entry_rules:          # 순서대로 평가, 먼저 참인 규칙의 방향
                if evaluate(rule["when"], resolver, sb):
                    side = 1 if rule["side"] == "long" else -1
                    break
        elif evaluate(preset.entry, resolver, sb):
            side = self.entry_side
        if side is None:
            return
        # passive-then-aggressive: 지정가(신호봉 종가)를 걸어두고 다음 봉들에서 체결/추격.
        if self.maker_timeout_bars > 0:
            self.pending = {"side": side, "limit": sig_close, "bars_left": self.maker_timeout_bars}
            return
        equity = ex.equity()
        lev = _leverage_for(preset.sizing, equity, cfg.max_leverage)   # 현재 자산 기준 레버리지
        p = _open_position(preset, preset.sizing, ex_block, sig_close, fill_time, sb,
                           side, lev, equity, cfg, atr_series, signal, entry_maker=self.maker_entry)
        if p is not None:
            ex.open(p)
            if self.on_open:
                self.on_open(p, lev)

    def _resolve_pending(self, signal, atr_series, hi, lo, cl, ot, sb):
        """대기 중 지정가 진입을 이 1분봉에서 판정 — 터치하면 maker 체결, 시간초과면 taker 추격."""
        pend = self.pending
        side, L = pend["side"], pend["limit"]
        touched = (lo <= L) if side == 1 else (hi >= L)   # 롱=지정가까지 내려오면, 숏=올라오면 체결
        if touched:
            self._fill_pending(L, ot, sb, side, signal, atr_series, is_maker=True)
            return
        pend["bars_left"] -= 1
        if pend["bars_left"] <= 0:                          # 시간초과 → 시장가로 추격(가격이 신호 방향으로 도망)
            self._fill_pending(cl, ot, sb, side, signal, atr_series, is_maker=False)

    def _fill_pending(self, price, ot, sb, side, signal, atr_series, is_maker):
        """대기 지정가 체결 확정 → 실제 포지션 진입(체결가·maker여부는 판정에서 결정됨)."""
        self.pending = None
        ex, cfg, preset = self.ex, self.cfg, self.preset
        if ex.position is not None:
            return
        if self.entry_gate is not None and not self.entry_gate():   # 체결 순간 게이트 닫힘 → 주문 취소
            return
        equity = ex.equity()
        lev = _leverage_for(preset.sizing, equity, cfg.max_leverage)
        p = _open_position(preset, preset.sizing, preset.exit, price, ot, sb,
                           side, lev, equity, cfg, atr_series, signal, entry_maker=is_maker)
        if p is not None:
            ex.open(p)
            if self.on_open:
                self.on_open(p, lev)


def run(base: Candles, preset: Preset, cfg: BacktestConfig = None) -> Metrics:
    cfg = cfg or BacktestConfig()
    tf_min = TIMEFRAME_MINUTES[preset.timeframe]

    signal = resample(base, tf_min)
    resolver = SeriesResolver(signal)
    atr_series = ind.atr(signal.high, signal.low, signal.close, 14)
    bar_of, is_close = signal_close_index(base, tf_min)

    # 주문 실행은 페이퍼 어댑터에 위임 — 라이브와 '같은 손익/수수료 계산'을 타게 된다.
    # (import를 함수 안에서: executor 가 metrics 를 쓰고 backtest 도 metrics 를 써서
    #  모듈 최상단에서 서로 물리면 순환이 된다.)
    from .executor import PaperExecutor
    ex = PaperExecutor(equity=cfg.initial_equity, taker_fee=cfg.taker_fee, maker_fee=cfg.maker_fee)

    trades = ex.trades                                    # Stepper가 청산할 때마다 append
    equity_curve = [(int(base.open_time[0]), cfg.initial_equity)]

    def on_close(trade):
        equity_curve.append((int(trade.exit_time), ex.equity()))

    stepper = Stepper(preset, cfg, ex, on_close=on_close)

    for t in range(len(base)):
        stepper.step(base, signal, bar_of, is_close, atr_series, resolver, t)
        # 마킹: 무포지션 구간도 자산곡선에 점 남김(선택)
        if ex.position is None and is_close[t]:
            equity_curve.append((int(base.open_time[t]), ex.equity()))

    # 종료 시 잔여 포지션 청산(마지막 종가 → 그 봉이 닫히는 순간). 백테스트에만 있는 꼬리 처리 —
    # 라이브는 데이터가 끝나지 않으므로 포지션을 그대로 들고 간다.
    if ex.position is not None:
        stepper._close(base.close[-1], "signal", int(base.open_time[-1]) + MINUTE_MS,
                       int(bar_of[-1]))

    return Metrics(cfg.initial_equity, ex.equity(), trades, equity_curve)


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


def _supertrend_flip_exit(resolver, st_exit: dict, side: int, sb: int) -> bool:
    """포지션 방향과 반대로 SuperTrend가 전환하면 True (추세이탈 익절).

    롱(+1): 상승(+1)→하락(-1) 전환 시 / 숏(-1): 하락(-1)→상승(+1) 전환 시.
    신호 타임프레임의 SUPERTREND_DIR(±1)을 직전 봉과 비교. 워밍업(NaN)이면 False.
    """
    if sb < 1:
        return False
    d = resolver.resolve({"indicator": "SUPERTREND_DIR",
                          "period": int(st_exit.get("period", 10)),
                          "params": {"multiplier": float(st_exit.get("multiplier", 3.0))}})
    d0, d1 = d[sb - 1], d[sb]
    if np.isnan(d0) or np.isnan(d1):
        return False
    return bool((d0 > 0 and d1 < 0) if side == 1 else (d0 < 0 and d1 > 0))


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


def _open_position(preset, sizing, ex, price, ot, sb, side, lev, equity, cfg, atr_series, signal,
                   entry_maker=False):
    atr_val = atr_series[sb]
    # 손절/익절가 먼저 계산 (리스크 사이징이 손절 거리를 필요로 함).
    # 롱 손절은 아래(-side), 익절은 위(+side). swing 타입은 side 무시.
    sl_cfg, tp_cfg = ex.get("stopLoss"), ex.get("takeProfit")
    stop_price = _exit_level(sl_cfg, price, -side, atr_val, signal, sb) if sl_cfg else float("nan")
    stop_price = _valid_level(stop_price, price, -side)      # 방향 뒤집힌 손절 무효화
    if tp_cfg and tp_cfg.get("type") == "riskReward":
        # 손익비(R:R): 익절 거리 = 손절 거리 × value. 손절 없으면 익절도 없음(nan).
        tp_price = (price + side * float(tp_cfg["value"]) * abs(price - stop_price)
                    if not np.isnan(stop_price) else float("nan"))
    else:
        tp_price = _exit_level(tp_cfg, price, side, atr_val, signal, sb) if tp_cfg else float("nan")
        tp_price = _valid_level(tp_price, price, side)       # 방향 뒤집힌 익절 무효화

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

    entry_fee = bm.trade_fee(price, qty, taker=not entry_maker,
                             taker_fee=cfg.taker_fee, maker_fee=cfg.maker_fee)
    return _Position(
        side=side, entry_time=ot, entry_price=price, qty=qty, leverage=lev,
        margin=margin, liq_price=liq, stop_price=stop_price, tp_price=tp_price,
        entry_fee=entry_fee, entry_signal_idx=sb, peak=price,
    )
