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
import json as _json
import os
import time
import urllib.request

import numpy as np


def notify(msg: str) -> None:
    """선택적 알림 — 환경변수 NOTIFY_WEBHOOK(Discord/Slack 호환 {content})로 POST. 없으면 무시.

    배포 시 봇이 죽거나 진입/청산할 때 텔레그램/디스코드로 받기 위함. stdlib만 사용.
    """
    url = os.environ.get("NOTIFY_WEBHOOK")
    if not url:
        return
    try:
        data = _json.dumps({"content": msg}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass    # 알림 실패가 트레이딩을 막으면 안 됨

from . import binance_math as bm
from . import candle_store
from . import control
from . import ledger
from . import indicators as ind
from .candles import resample, signal_close_index, TIMEFRAME_MINUTES, MINUTE_MS
from .conditions import SeriesResolver, evaluate
from .preset import Preset, load_preset_file
from .executor import PaperExecutor, LiveExecutor
from .backtest import (
    BacktestConfig, _leverage_for, _open_position, _supertrend_flip_exit,
    _trailing_hit, _trailing_stop, _entry_allowed,
)


class LiveTrader:
    """실시간 봉 스트림(폴링)을 백테스트와 같은 로직으로 처리해 Executor에 주문."""

    def __init__(self, preset: Preset, executor, cfg: BacktestConfig = None, warmup_days: float = 10,
                 state_path: str = "data/state.json", strategy_path: str = None,
                 mode: str = "paper", ledger_path: str = None):
        self.preset = preset
        self.ex = executor
        self.cfg = cfg or BacktestConfig()
        self.warmup_days = warmup_days
        self.state_path = state_path         # 대시보드가 읽을 상태 스냅샷(포지션·트레이드·잔고)
        self.strategy_path = strategy_path   # 현재 활성 전략 파일 경로(대시보드 선택과 비교)
        self.mode = mode                     # 'paper' | 'live' — 원장 분리 저장
        self.ledger_path = ledger_path or ledger.LEDGER_PATH
        self._pending_strategy = None        # 전환 대기(포지션 청산 후 적용할 전략)
        self._strategy_error = None
        self._apply_derived(preset)          # tf_min/entry_rules/entry_side/maker_entry
        self._last_ot = None                 # 마지막으로 처리한 1분봉 open_time
        self._last_exit_sb = -10 ** 9        # 쿨다운용
        self._started_at = int(time.time() * 1000)
        self._paused = False                 # control.json에서 읽음(멈춤=새 진입 차단)
        self._restore_from_ledger()          # 원장에서 잔고·이력 복원(재시작해도 안 사라짐)

    def _restore_from_ledger(self):
        """원장(같은 mode)에서 과거 거래를 읽어 잔고·트레이드 이력 복원.
        페이퍼: 잔고 = 초기 + 누적손익. (포지션은 복원 안 함 — 플랫 시작; 실거래는 거래소서 동기화)"""
        from .executor import ClosedTrade
        rows = ledger.load(self.ledger_path, mode=self.mode)
        if not rows:
            return
        restored = [ClosedTrade(
            side=r["side"], entry_time=r["entry_time"], entry_price=r["entry_price"],
            exit_time=r["exit_time"], exit_price=r["exit_price"], qty=r["qty"],
            leverage=r["leverage"], pnl=r["pnl"], fees=r["fees"], funding=r["funding"],
            reason=r["reason"]) for r in rows]
        if hasattr(self.ex, "trades"):
            self.ex.trades = restored
        if hasattr(self.ex, "_equity"):
            self.ex._equity = self.cfg.initial_equity + sum(r["pnl"] for r in rows)
        print(f"원장 복원: {len(rows)}건, 잔고 {self.ex.equity():.2f} ({self.mode})", flush=True)

    def _apply_derived(self, preset: Preset):
        """프리셋에서 파생되는 실행 파라미터 세팅(전환 시 재호출)."""
        self.tf_min = TIMEFRAME_MINUTES[preset.timeframe]
        self.entry_rules = preset.data.get("entryRules")
        self.entry_side = -1 if preset.direction == "short" else 1
        execution = preset.data.get("execution") or {}
        self.maker_entry = execution.get("entryType") == "makerLimit"

    def _maybe_switch_strategy(self):
        """대시보드가 고른 '원하는 전략'을 확인 → 무포지션이면 전환, 포지션 있으면 대기.
        (1번 방식: 안전 — 열린 포지션은 기존 전략이 청산할 때까지 그대로 두고 flat 되면 교체.)"""
        desired = control.get_strategy()
        if not desired or desired == self.strategy_path:
            self._pending_strategy = None
            return
        if self.ex.position is not None:     # 포지션 열림 → 청산 후로 미룸
            self._pending_strategy = desired
            return
        try:
            new = load_preset_file(desired, validate=True)
        except Exception as e:
            self._strategy_error = f"{desired}: {e}"
            self._pending_strategy = desired
            notify(f"⚠️ 전략 전환 실패 {desired}: {e}")
            print(f"  [전략전환 실패] {e}", flush=True)
            return
        self._apply_strategy(new, desired)

    def _apply_strategy(self, preset: Preset, path: str):
        """무포지션 상태에서 전략을 실제로 갈아끼운다(심볼 바뀌면 수수료·데이터도 갱신)."""
        old = self.preset.name
        self.preset = preset
        self.strategy_path = path
        self._apply_derived(preset)
        mk, tk = bm.fees_for_symbol(preset.symbol)   # 심볼 바뀌면 수수료 갱신
        self.cfg.maker_fee, self.cfg.taker_fee = mk, tk
        if hasattr(self.ex, "maker_fee"):
            self.ex.maker_fee, self.ex.taker_fee = mk, tk
        self._last_exit_sb = -10 ** 9                # 쿨다운 리셋
        self._pending_strategy = None
        self._strategy_error = None
        # 과거 replay 방지: 새 전략/심볼 데이터의 최신 닫힌 봉으로 _last_ot 세팅
        self._last_ot = None
        try:
            base = self._fetch(int(time.time() * 1000))
            if len(base):
                self._last_ot = int(base.open_time[-1])
        except Exception:
            pass
        notify(f"🔄 전략 전환 {old} → {preset.name} ({preset.symbol} {preset.timeframe})")
        print(f"  [전략전환] {old} → {preset.name} ({preset.symbol} {preset.timeframe})", flush=True)

    def _write_state(self):
        """포지션·트레이드·잔고 스냅샷을 state_path에 원자적으로 기록(대시보드용)."""
        if not self.state_path:
            return
        ex = self.ex
        pos = ex.position

        def _px(x):
            return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 2)

        trades = getattr(ex, "trades", [])
        state = {
            "preset": self.preset.name, "symbol": self.preset.symbol, "timeframe": self.preset.timeframe,
            "startedAt": self._started_at, "updatedAt": int(time.time() * 1000),
            "paused": self._paused,
            "mode": self.mode,                           # paper | live (원장 조회용)
            "strategy": self.strategy_path,              # 현재 활성 전략 파일 경로
            "pendingStrategy": self._pending_strategy,   # 전환 대기(포지션 청산 후) 경로 or None
            "strategyError": self._strategy_error,
            "equity": round(ex.equity(), 2), "initialEquity": round(self.cfg.initial_equity, 2),
            "returnPct": round((ex.equity() / self.cfg.initial_equity - 1) * 100, 2),
            "numTrades": len(trades),
            "position": None if pos is None else {
                "side": pos.side, "entryPrice": _px(pos.entry_price), "qty": round(pos.qty, 6),
                "leverage": pos.leverage, "stop": _px(pos.stop_price), "tp": _px(pos.tp_price),
                "liq": _px(pos.liq_price), "entryTime": int(pos.entry_time)},
            "trades": [{
                "side": t.side, "entryTime": int(t.entry_time), "entryPrice": _px(t.entry_price),
                "exitTime": int(t.exit_time), "exitPrice": _px(t.exit_price),
                "pnl": round(t.pnl, 2), "reason": t.reason} for t in trades[-100:]],
        }
        try:
            import os
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)     # 원자적 교체
        except Exception as e:
            print(f"  [상태기록 실패] {e}", flush=True)

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
            self._maybe_switch_strategy()    # 폴링 시작 시 전략 전환 확인(무포지션이면 교체)
            if now_ms is None:
                raise ValueError("now_ms 필요(실시간). 테스트는 base 주입.")
            base = self._fetch(now_ms)
        if len(base) < 2:
            return []
        self._paused = control.service_state("trader") == "paused"   # 멈춤이면 새 진입 차단
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

        # 진입 (멈춤 상태면 새 진입 안 함 — 기존 포지션은 위에서 계속 관리됨)
        if ex.position is None and not self._paused \
                and _entry_allowed(sb, fill_time, self.preset.filter, self._last_exit_sb, cfg):
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
        try:                             # 원장에 append(영구 기록) — 실패해도 트레이딩은 계속
            ledger.record(tr, symbol=self.preset.symbol,
                          strategy=self.strategy_path or self.preset.name,
                          mode=self.mode, equity_after=self.ex.equity(), db_path=self.ledger_path)
        except Exception as e:
            print(f"  [원장기록 실패] {e}", flush=True)
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
        self._write_state()
        notify(f"▶️ 페이퍼 시작 {self.preset.name} {self.preset.symbol} {self.preset.timeframe} 잔고 {self.ex.equity():.0f}")
        fails = 0
        while True:
            now = int(time.time() * 1000)
            try:
                events = self.poll_once(now_ms=now)
                self._write_state()
                for e in events:
                    print(f"  [{e['type']}] {e}", flush=True)
                    if e["type"] == "open":
                        notify(f"🟢 진입 {'롱' if e['side']>0 else '숏'} @{e['price']:.2f} x{e['lev']} ({self.preset.symbol})")
                    elif e["type"] == "close":
                        notify(f"🔴 청산 {e['reason']} @{e['price']:.2f} pnl {e['pnl']:+.2f} 잔고 {self.ex.equity():.0f}")
                st = f"잔고 {self.ex.equity():.2f}"
                pos = self.ex.position
                st += f" | 포지션 {'롱' if pos.side>0 else '숏'} @{pos.entry_price:.2f}" if pos else " | 무포지션"
                print(f"{time.strftime('%H:%M:%S')}  {st}", flush=True)
                fails = 0
            except Exception as e:
                fails += 1
                print(f"  [에러] {e}", flush=True)
                if fails in (1, 5, 20):      # 반복 실패 시 알림(스팸 방지)
                    notify(f"⚠️ 에러({fails}회): {e}")
            if once:
                break
            time.sleep(interval)


def main():
    from .env import load_dotenv
    load_dotenv()                            # .env → 환경변수(BINANCE_API_KEY 등)
    ap = argparse.ArgumentParser(description="페이퍼/실거래 트레이딩 루프 (뼈대)")
    ap.add_argument("preset", help="프리셋 JSON 경로 (presets/saved/... 또는 examples/...)")
    ap.add_argument("--paper", action="store_true", help="페이퍼 트레이딩(기본). 실거래는 --live(미구현)")
    ap.add_argument("--live", action="store_true", help="실거래 — 아직 미구현(NotImplementedError)")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--interval", type=int, default=60, help="폴링 간격(초)")
    ap.add_argument("--once", action="store_true", help="한 번만 폴링하고 종료(테스트용)")
    args = ap.parse_args()

    preset = load_preset_file(args.preset, validate=True)
    mk, tk = bm.fees_for_symbol(preset.symbol)
    cfg = BacktestConfig(initial_equity=args.equity, maker_fee=mk, taker_fee=tk)
    if args.live:
        ex = LiveExecutor()              # NotImplementedError
    else:
        ex = PaperExecutor(equity=args.equity, maker_fee=mk, taker_fee=tk)
    trader = LiveTrader(preset, ex, cfg, strategy_path=args.preset,
                        mode="live" if args.live else "paper")
    print(f"[{'페이퍼' if not args.live else '실거래'}] {preset.name} · {preset.symbol} {preset.timeframe} "
          f"· 수수료 maker {mk*100:.3f}%/taker {tk*100:.3f}% · 초기잔고 {args.equity:.0f}")
    trader.run(interval=args.interval, once=args.once)


if __name__ == "__main__":
    main()
