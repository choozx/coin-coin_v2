"""실거래/페이퍼 트레이딩 루프 — 백테스트와 '같은 판정 로직', 실시간 클럭.

설계(이 프로젝트 대전제): 전략 엔진(지표·조건·사이징·청산·수수료)은 backtest.py의 것을
그대로 import해 재사용하고, 주문 실행만 Executor(페이퍼/실거래)로 갈아끼운다.

흐름:
  1) 1분봉을 주기 폴링(candle_store) → 최신 '닫힌' 봉까지 확보
  2) 상위 TF 리샘플 → 매 1분봉마다 포지션 관리(펀딩·청산·손절·익절),
     신호봉 닫히면 진입/청산 신호 판정 (backtest.run() 루프와 동일 순서)
  3) 결정은 Executor로 실행 (PaperExecutor=시뮬 / LiveExecutor=ccxt, TODO)

⚠️ 지금은 뼈대: backtest.run()의 per-bar 로직을 여기서 '같은 순서로' 다시 호출한다
   (primitives는 공유하므로 사이징·청산가·플립 판정은 안 갈리지만, 오케스트레이션 순서는
   중복). 다음 단계: backtest.run()에서 per-bar step()을 추출해 backtest/live가 문자 그대로
   공유하도록 리팩터.
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from . import binance_math as bm
from . import candle_store
from . import indicators as ind
from .candles import resample, signal_close_index, TIMEFRAME_MINUTES, MINUTE_MS
from .conditions import SeriesResolver, evaluate
from .preset import Preset
from .executor import PaperExecutor, LiveExecutor
from .backtest import (
    BacktestConfig, _leverage_for, _open_position, _supertrend_flip_exit,
    _trailing_hit, _trailing_stop, _entry_allowed,
)


class LiveTrader:
    """실시간 봉 스트림(폴링)을 백테스트와 같은 로직으로 처리해 Executor에 주문."""

    def __init__(self, preset: Preset, executor, cfg: BacktestConfig = None, warmup_days: float = 10):
        self.preset = preset
        self.ex = executor
        self.cfg = cfg or BacktestConfig()
        self.warmup_days = warmup_days
        self.tf_min = TIMEFRAME_MINUTES[preset.timeframe]
        self.entry_rules = preset.data.get("entryRules")
        self.entry_side = -1 if preset.direction == "short" else 1
        execution = preset.data.get("execution") or {}
        self.maker_entry = execution.get("entryType") == "makerLimit"
        self._last_ot = None                 # 마지막으로 처리한 1분봉 open_time
        self._last_exit_sb = -10 ** 9        # 쿨다운용

    # ---- 데이터 확보 (실시간: 최신 닫힌 봉까지) ----
    def _fetch(self, now_ms: int):
        """최신 1분봉을 캐시에 채우고, '닫힌' 봉까지의 Candles 반환. (진행 중 봉 제외)"""
        base = candle_store.ensure_days(self.preset.symbol, self.warmup_days,
                                        end_ms=now_ms, verbose=False)
        # 진행 중(아직 안 닫힌) 마지막 봉 제거: open_time + 1분 > now 이면 미완성.
        n = len(base)
        while n > 0 and int(base.open_time[n - 1]) + MINUTE_MS > now_ms:
            n -= 1
        if n < len(base):
            base = candle_store.load_range(self.preset.symbol,
                                           int(base.open_time[0]), int(base.open_time[n - 1]))
        return base

    def poll_once(self, now_ms: int = None, base=None):
        """한 번 폴링 → 새로 닫힌 1분봉들을 순서대로 처리. 반환: 이번에 발생한 이벤트 리스트."""
        if base is None:
            if now_ms is None:
                raise ValueError("now_ms 필요(실시간). 테스트는 base 주입.")
            base = self._fetch(now_ms)
        if len(base) < 2:
            return []
        signal = resample(base, self.tf_min)
        bar_of, is_close = signal_close_index(base, self.tf_min)
        atr_series = ind.atr(signal.high, signal.low, signal.close, 14)
        resolver = SeriesResolver(signal)

        events = []
        # 아직 처리 안 한 1분봉만 (갭이 있어도 순서대로 따라잡음)
        start = 0
        if self._last_ot is not None:
            idx = np.searchsorted(base.open_time, self._last_ot, side="right")
            start = int(idx)
        for t in range(start, len(base)):
            self._step(base, signal, bar_of, is_close, atr_series, resolver, t, events)
            self._last_ot = int(base.open_time[t])
        return events

    # ---- per-bar 처리 (backtest.run() 루프와 동일 순서: 펀딩→청산→손절→익절→신호) ----
    def _step(self, base, signal, bar_of, is_close, atr_series, resolver, t, events):
        ex, cfg = self.ex, self.cfg
        ot = int(base.open_time[t]); hi, lo, cl = base.high[t], base.low[t], base.close[t]
        sb = int(bar_of[t])                  # 이 1분봉이 속한 신호봉 인덱스(쿨다운·청산사유용)
        pos = ex.position

        if pos is not None:
            if bm.is_funding_time(ot):
                ex.accrue_funding(cl, cfg.funding_rate)
            # 청산(손절보다 먼저)
            liq = (pos.side == 1 and lo <= pos.liq_price) or (pos.side == -1 and hi >= pos.liq_price)
            if liq:
                self._do_close(pos.liq_price, "liquidation", ot, False, sb, events); return
            # 손절 / 트레일링 / 익절
            trailing = self.preset.exit.get("trailing")
            if pos.side == 1:
                pos.peak = max(pos.peak, hi)
                if not np.isnan(pos.stop_price) and lo <= pos.stop_price:
                    self._do_close(pos.stop_price, "stop_loss", ot, False, sb, events); return
                if trailing and _trailing_hit(pos, trailing, lo, hi):
                    self._do_close(_trailing_stop(pos, trailing), "trailing", ot, False, sb, events); return
                if not np.isnan(pos.tp_price) and hi >= pos.tp_price:
                    self._do_close(pos.tp_price, "take_profit", ot, True, sb, events); return
            else:
                pos.peak = min(pos.peak, lo)
                if not np.isnan(pos.stop_price) and hi >= pos.stop_price:
                    self._do_close(pos.stop_price, "stop_loss", ot, False, sb, events); return
                if trailing and _trailing_hit(pos, trailing, lo, hi):
                    self._do_close(_trailing_stop(pos, trailing), "trailing", ot, False, sb, events); return
                if not np.isnan(pos.tp_price) and lo <= pos.tp_price:
                    self._do_close(pos.tp_price, "take_profit", ot, True, sb, events); return

        if not is_close[t]:
            return
        sig_close = signal.close[sb]; fill_time = ot + MINUTE_MS
        ex_block = self.preset.exit

        # 신호 기반 청산 (SuperTrend 전환 / 지표조건 / 시간)
        if ex.position is not None:
            pos = ex.position
            st_exit = ex_block.get("supertrendExit")
            cond = ex_block.get("condition"); time_stop = ex_block.get("timeStop")
            if st_exit is not None and _supertrend_flip_exit(resolver, st_exit, pos.side, sb):
                reason = {"stopLoss": "stop_loss", "exit": "supertrend"}.get(st_exit.get("as"), "take_profit")
                self._do_close(sig_close, reason, fill_time, self.maker_entry, sb, events); return
            if cond is not None and evaluate(cond, resolver, sb):
                self._do_close(sig_close, "signal", fill_time, False, sb, events); return
            if time_stop is not None and (sb - pos.entry_signal_idx) >= time_stop["maxBars"]:
                self._do_close(sig_close, "time", fill_time, False, sb, events); return

        # 진입
        if ex.position is None and _entry_allowed(sb, fill_time, self.preset.filter, self._last_exit_sb, cfg):
            side = None
            if self.entry_rules:
                for rule in self.entry_rules:
                    if evaluate(rule["when"], resolver, sb):
                        side = 1 if rule["side"] == "long" else -1
                        break
            elif evaluate(self.preset.entry, resolver, sb):
                side = self.entry_side
            if side is not None:
                lev = _leverage_for(self.preset.sizing, ex.equity(), cfg.max_leverage)
                p = _open_position(self.preset, self.preset.sizing, ex_block, sig_close, fill_time, sb,
                                   side, lev, ex.equity(), cfg, atr_series, signal, entry_maker=self.maker_entry)
                if p is not None:
                    ex.open(p)
                    events.append({"type": "open", "side": side, "price": sig_close, "time": fill_time,
                                   "qty": p.qty, "lev": lev, "stop": p.stop_price, "tp": p.tp_price})

    def _do_close(self, price, reason, ts, is_maker, sb, events):
        tr = self.ex.close(price, reason, ts, is_maker=is_maker)
        self._last_exit_sb = sb          # 쿨다운 기준 신호봉(backtest last_exit_signal_idx와 동일)
        events.append({"type": "close", "reason": reason, "price": price, "time": ts, "pnl": round(tr.pnl, 2)})

    def bootstrap(self, now_ms: int = None):
        """라이브 시작 시: 지표 워밍업만 하고 최신봉까지 건너뛴다(과거 신호 실행 안 함, 플랫 시작).

        (poll_once는 _last_ot 이후만 처리하므로, _last_ot을 최신봉으로 세팅해 과거 replay 방지.)
        """
        now_ms = now_ms or int(time.time() * 1000)
        base = self._fetch(now_ms)
        if len(base):
            self._last_ot = int(base.open_time[-1])
        print(f"부트스트랩: {len(base)}봉 워밍업, 플랫으로 시작 → 이후 새로 닫히는 봉만 실행.", flush=True)

    def run(self, interval: int = 60, once: bool = False):
        """폴링 루프. once=True면 한 번만. interval초마다 poll_once(now)."""
        self.bootstrap()
        while True:
            now = int(time.time() * 1000)
            try:
                events = self.poll_once(now_ms=now)
                for e in events:
                    print(f"  [{e['type']}] {e}", flush=True)
                st = f"잔고 {self.ex.equity():.2f}"
                pos = self.ex.position
                st += f" | 포지션 {'롱' if pos.side>0 else '숏'} @{pos.entry_price:.2f}" if pos else " | 무포지션"
                print(f"{time.strftime('%H:%M:%S')}  {st}", flush=True)
            except Exception as e:
                print(f"  [에러] {e}", flush=True)
            if once:
                break
            time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(description="페이퍼/실거래 트레이딩 루프 (뼈대)")
    ap.add_argument("preset", help="프리셋 JSON 경로 (presets/saved/... 또는 examples/...)")
    ap.add_argument("--paper", action="store_true", help="페이퍼 트레이딩(기본). 실거래는 --live(미구현)")
    ap.add_argument("--live", action="store_true", help="실거래 — 아직 미구현(NotImplementedError)")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--interval", type=int, default=60, help="폴링 간격(초)")
    ap.add_argument("--once", action="store_true", help="한 번만 폴링하고 종료(테스트용)")
    args = ap.parse_args()

    import json
    with open(args.preset, encoding="utf-8") as f:
        raw = json.load(f)
    # presets/saved/ 파일은 {name, form, params, preset} 래퍼 → preset 키 사용. 아니면 raw 자체.
    preset = Preset.from_dict(raw.get("preset", raw), validate=True)
    mk, tk = bm.fees_for_symbol(preset.symbol)
    cfg = BacktestConfig(initial_equity=args.equity, maker_fee=mk, taker_fee=tk)
    if args.live:
        ex = LiveExecutor()              # NotImplementedError
    else:
        ex = PaperExecutor(equity=args.equity, maker_fee=mk, taker_fee=tk)
    trader = LiveTrader(preset, ex, cfg)
    print(f"[{'페이퍼' if not args.live else '실거래'}] {preset.name} · {preset.symbol} {preset.timeframe} "
          f"· 수수료 maker {mk*100:.3f}%/taker {tk*100:.3f}% · 초기잔고 {args.equity:.0f}")
    trader.run(interval=args.interval, once=args.once)


if __name__ == "__main__":
    main()
