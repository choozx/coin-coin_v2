"""실거래/페이퍼 트레이딩 루프 — 백테스트와 '같은 판정 로직', 실시간 클럭.

설계(이 프로젝트 대전제): 전략 엔진(지표·조건·사이징·청산·수수료)은 backtest.py의 것을
그대로 import해 재사용하고, 주문 실행만 Executor(페이퍼/실거래)로 갈아끼운다.

흐름:
  1) 1분봉을 주기 폴링(candle_store) → 최신 '닫힌' 봉까지 확보
  2) 상위 TF 리샘플 → 봉마다 backtest.Stepper.step() 호출
  3) 결정은 Executor로 실행 (PaperExecutor=시뮬 / LiveExecutor=ccxt, 주문은 미구현)

판정 로직(펀딩→청산→손절/익절→신호→진입)은 **여기 없다**. backtest.Stepper 한 곳에만 있고
백테스트와 문자 그대로 같은 코드를 탄다. 이 파일이 담당하는 건 라이브 고유의 것들:
실시간 클럭·폴링, 전략/봇설정 핫스왑, 멈춤·리스크 가드레일(진입 게이트), 원장 기록,
대시보드 상태 스냅샷. 두 경로가 갈라지지 않는지는 tests/test_backtest_live_parity.py 가 지킨다.
"""
from __future__ import annotations

import argparse
import copy
import json as _json
import os
import time
import urllib.request

import numpy as np


def notify(msg: str) -> None:
    """선택적 알림 — 환경변수 NOTIFY_WEBHOOK로 POST. 없으면 무시.

    배포 시 봇이 죽거나 진입/청산할 때 Discord/Slack으로 받기 위함. stdlib만 사용.
    웹훅 종류마다 payload 키가 다르다(Slack=text, Discord=content) — URL로 판별해 맞춰 보낸다.
    (content 로만 보내면 Slack incoming 웹훅은 invalid_payload 로 거부한다.)
    """
    url = os.environ.get("NOTIFY_WEBHOOK")
    if not url:
        return
    if "hooks.slack.com" in url:
        payload = {"text": msg}
    elif "discord" in url:                       # discord.com / discordapp.com
        payload = {"content": msg}
    else:
        payload = {"content": msg, "text": msg}  # 알 수 없는 웹훅이면 둘 다(각자 모르는 키는 무시)
    try:
        data = _json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass    # 알림 실패가 트레이딩을 막으면 안 됨

from . import binance_math as bm
from . import candle_store
from . import control
from . import settings
from . import ledger
from . import indicators as ind
from .candles import resample, signal_close_index, TIMEFRAME_MINUTES, MINUTE_MS
from .conditions import SeriesResolver
from .preset import Preset, load_preset_file, merge_bot_config
from .executor import PaperExecutor, LiveExecutor
from .backtest import BacktestConfig, Stepper


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
        self._base_data = copy.deepcopy(preset.data)   # 프리셋 원본(신호 소스) — 봇설정과 병합
        self._bot_cfg = {}                   # 마지막 적용한 봇 설정
        self._rebuild_effective()            # 신호(프리셋) + 봇설정(심볼·사이징·실행·필터) 병합
        self._last_ot = None                 # 마지막으로 처리한 1분봉 open_time
        self._started_at = int(time.time() * 1000)
        self._paused = False                 # control.json에서 읽음(멈춤=새 진입 차단)
        self._events = []                    # 이번 폴링에서 발생한 이벤트(hook이 채움)
        self._guardrail_reason = None        # 리스크 가드레일 발동 사유(없으면 None)
        self._restore_from_ledger()          # 원장에서 잔고·이력 복원(재시작해도 안 사라짐)

    def _restore_from_ledger(self):
        """원장(같은 mode)에서 과거 거래를 읽어 잔고·트레이드 이력 복원.

        페이퍼: 잔고 = 초기 + 누적손익.
        실거래: 잔고의 진실은 거래소다 → 반대로 **기준잔고**(수익률 표시의 분모)를
                '현재 실잔고 − 누적 실현손익' 으로 역산한다(재시작해도 수익률이 안 리셋).
        포지션은 여기서 복원하지 않는다 — 실거래는 bootstrap 에서 거래소와 동기화.
        """
        from .executor import ClosedTrade
        rows = ledger.load(self.ledger_path, mode=self.mode)
        restored = [ClosedTrade(
            side=r["side"], entry_time=r["entry_time"], entry_price=r["entry_price"],
            exit_time=r["exit_time"], exit_price=r["exit_price"], qty=r["qty"],
            leverage=r["leverage"], pnl=r["pnl"], fees=r["fees"], funding=r["funding"],
            exit_reason=r["reason"]) for r in rows]      # DB 컬럼명은 reason 유지
        paper = hasattr(self.ex, "_equity")
        if paper and not rows:
            return
        if hasattr(self.ex, "trades") and rows:
            self.ex.trades = restored
        if paper:
            self.ex._equity = self.cfg.initial_equity + sum(r["pnl"] for r in rows)
        else:
            self.cfg.initial_equity = max(1e-9, self.ex.equity() - sum(r["pnl"] for r in rows))
        print(f"원장 복원: {len(rows)}건, 잔고 {self.ex.equity():.2f} "
              f"(기준 {self.cfg.initial_equity:.2f}, {self.mode})", flush=True)

    def _apply_derived(self, preset: Preset):
        """프리셋에서 파생되는 실행 파라미터 세팅(전환 시 재호출).

        진입규칙·방향·maker 여부는 Stepper 가 들고 있다(판정하는 쪽이 소유) —
        트레이더는 폴링에 필요한 tf_min 만 유지한다.
        """
        self.tf_min = TIMEFRAME_MINUTES[preset.timeframe]
        if getattr(self, "stepper", None) is None:
            self.stepper = Stepper(preset, self.cfg, self.ex,
                                   entry_gate=self._entry_gate,
                                   on_open=self._on_open, on_close=self._on_close)
        else:
            self.stepper.apply_preset(preset)      # 쿨다운(last_exit_sb)은 유지

    def _rebuild_effective(self):
        """base 프리셋(신호: tf·진입·청산·방향) + 현재 봇 설정(심볼·사이징·실행·필터)을
        병합해 유효 프리셋을 만든다. 봇 설정이 무효면 프리셋 값으로 폴백."""
        self._bot_cfg = control.get_bot_config()
        merged = merge_bot_config(self._base_data, self._bot_cfg)
        # 동적 레버리지: 봇 설정이 켜져 있으면 글로벌 티어 주입, 명시적으로 끄면 티어 제거(고정 사용)
        dyn = self._bot_cfg.get("useDynamicLeverage")
        if dyn is True:
            merged.setdefault("sizing", {})["leverageTiers"] = settings.get_leverage_tiers()
        elif dyn is False:
            merged.setdefault("sizing", {}).pop("leverageTiers", None)
        try:
            self.preset = Preset.from_dict(merged, validate=True)
        except Exception as e:
            print(f"  [봇설정 무효 → 프리셋 값 사용] {e}", flush=True)
            self.preset = Preset(copy.deepcopy(self._base_data))
        self._apply_derived(self.preset)
        mk, tk = bm.fees_for_symbol(self.preset.symbol)      # 심볼 바뀌면 수수료 갱신
        self.cfg.maker_fee, self.cfg.taker_fee = mk, tk
        if hasattr(self.ex, "maker_fee"):
            self.ex.maker_fee, self.ex.taker_fee = mk, tk
        if hasattr(self.ex, "set_symbol"):
            self.ex.set_symbol(self.preset.symbol)           # 실거래: 주문 대상 심볼도 함께(안 하면 옛 심볼로 주문)

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

    def _guardrail_block(self):
        """글로벌 리스크 가드레일 — 걸리면 사유(str), 아니면 None. 새 진입만 막음(청산·관리는 계속)."""
        g = settings.get_guardrails()
        if g.get("killSwitch"):
            return "킬스위치"
        trades = getattr(self.ex, "trades", []) or []
        mcl = g.get("maxConsecutiveLosses") or {}
        if mcl.get("enabled") and mcl.get("count"):
            streak = 0
            for tr in reversed(trades):
                if tr.pnl < 0:
                    streak += 1
                else:
                    break
            if streak >= int(mcl["count"]):
                return f"연속 손실 {streak}회"
        dll = g.get("dailyLossLimit") or {}
        if dll.get("enabled") and dll.get("pct"):
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            today_pnl = sum(tr.pnl for tr in trades if tr.exit_time and
                            datetime.fromtimestamp(tr.exit_time / 1000, timezone.utc).strftime("%Y-%m-%d") == today)
            if today_pnl < 0:
                base = self.ex.equity() - today_pnl      # 오늘 시작 잔고(= 현재잔고 - 오늘실현손익)
                loss_pct = (-today_pnl / base * 100) if base > 0 else 0
                if loss_pct >= float(dll["pct"]):
                    return f"일일 손실 {loss_pct:.1f}% (한도 {dll['pct']}%)"
        return None

    def _check_guardrail(self):
        """가드레일 상태 갱신 + 상태 변화 시 1회 알림. 반환: 발동 사유 or None."""
        gr = self._guardrail_block()
        if gr != self._guardrail_reason:
            self._guardrail_reason = gr
            if gr:
                notify(f"🛡 리스크 가드레일 발동 — 새 진입 차단: {gr}")
                print(f"  [가드레일] {gr} → 새 진입 차단", flush=True)
            else:
                print("  [가드레일] 해제 → 진입 재개", flush=True)
        return gr

    def _maybe_apply_bot_config(self):
        """대시보드가 '봇 설정'(심볼·사이징·레버리지·실행·필터)을 바꾸면 반영.
        무포지션일 때만 — 포지션 관리 중엔 파라미터가 안 바뀌게(안전)."""
        if control.get_bot_config() == self._bot_cfg:
            return
        if self.ex.position is not None:
            return                           # 포지션 있으면 청산 후 다음 폴링에 반영
        old_sym = self.preset.symbol
        self._rebuild_effective()
        if self.preset.symbol != old_sym:    # 심볼 바뀌면 과거 replay 방지
            self._last_ot = None
        self.stepper.last_exit_sb = -10 ** 9
        s = self.preset.sizing
        notify(f"⚙️ 봇 설정 반영 — {self.preset.symbol} lev{s.get('leverage')}")
        print(f"  [봇설정 반영] {self.preset.symbol} · lev{s.get('leverage')} · "
              f"{(s.get('size') or {}).get('type')} · maker={self.stepper.maker_entry}", flush=True)

    def _apply_strategy(self, preset: Preset, path: str):
        """무포지션 상태에서 전략을 실제로 갈아끼운다(심볼 바뀌면 수수료·데이터도 갱신)."""
        old = self.preset.name
        self._base_data = copy.deepcopy(preset.data)   # 새 신호 소스
        self.strategy_path = path
        self._rebuild_effective()                      # 신호 교체 + 봇설정 재적용(심볼·수수료 포함)
        self.stepper.last_exit_sb = -10 ** 9         # 쿨다운 리셋
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
        notify(f"🔄 전략 전환 {old} → {self.preset.name} ({self.preset.symbol} {self.preset.timeframe})")
        print(f"  [전략전환] {old} → {self.preset.name} ({self.preset.symbol} {self.preset.timeframe})", flush=True)

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
            "guardrail": self._guardrail_reason,         # 가드레일 발동 사유(대시보드 표시), 없으면 null
            "mode": self.mode,                           # paper | live (원장 조회용)
            "testnet": bool(getattr(ex, "testnet", False)),   # live 중에도 가짜돈인지 — 대시보드가 구분 표시
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
                "pnl": round(t.pnl, 2), "reason": t.exit_reason} for t in trades[-100:]],
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
            self._maybe_apply_bot_config()   # 봇 설정(심볼·사이징·실행·필터) 변경 반영(무포지션이면)
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

        self._events = []                # hook(_on_open/_on_close)이 여기에 쌓는다
        # 아직 처리 안 한 1분봉만 (갭이 있어도 순서대로 따라잡음)
        start = 0
        if self._last_ot is not None:
            idx = np.searchsorted(base.open_time, self._last_ot, side="right")
            start = int(idx)
        for t in range(start, len(base)):
            self.stepper.step(base, signal, bar_of, is_close, atr_series, resolver, t)
            self._last_ot = int(base.open_time[t])
        return self._events

    # ---- per-bar 처리 ----
    # 판정 로직은 backtest.Stepper 한 곳에만 있다(백테스트와 문자 그대로 같은 코드).
    # 여기 남는 건 '결과를 어떻게 기록할지' 뿐 — 이벤트 발행·원장 append·진입 게이트.

    def _entry_gate(self) -> bool:
        """새 진입 허용 여부. 멈춤·리스크 가드레일은 진입만 막고 기존 포지션 관리는 계속."""
        return not self._paused and self._check_guardrail() is None

    def _on_open(self, pos, lev):
        self._events.append({"type": "open", "side": pos.side, "price": pos.entry_price,
                             "time": pos.entry_time, "qty": pos.qty, "lev": lev,
                             "stop": pos.stop_price, "tp": pos.tp_price})

    def _on_close(self, trade):
        try:                             # 원장에 append(영구 기록) — 실패해도 트레이딩은 계속
            ledger.record(trade, symbol=self.preset.symbol,
                          strategy=self.strategy_path or self.preset.name,
                          mode=self.mode, equity_after=self.ex.equity(), db_path=self.ledger_path)
        except Exception as e:
            print(f"  [원장기록 실패] {e}", flush=True)
        self._events.append({"type": "close", "reason": trade.exit_reason, "price": trade.exit_price,
                             "time": trade.exit_time, "pnl": round(trade.pnl, 2)})

    def bootstrap(self, now_ms: int = None):
        """라이브 시작 시: 지표 워밍업만 하고 최신봉까지 건너뛴다(과거 신호 실행 안 함).

        (poll_once는 _last_ot 이후만 처리하므로, _last_ot을 최신봉으로 세팅해 과거 replay 방지.)
        실거래는 '플랫으로 시작'이 아니라 **거래소에 남아 있는 포지션을 이어받는다.**
        """
        now_ms = now_ms or int(time.time() * 1000)
        base = self._fetch(now_ms)
        if len(base):
            self._last_ot = int(base.open_time[-1])
        if self.mode == "live" and hasattr(self.ex, "sync_position"):
            self._sync_live_position(base)
        start = "플랫으로" if self.ex.position is None else "포지션 인계받아"
        print(f"부트스트랩: {len(base)}봉 워밍업, {start} 시작 → 이후 새로 닫히는 봉만 실행.", flush=True)

    def _sync_live_position(self, base):
        """재시작 시 거래소의 실제 포지션을 진실로 삼아 엔진 상태를 맞춘다.

        거래소는 손절/익절가·최고가(트레일링) 를 모른다 — 그건 우리가 진입 때 남긴 사이드카에서
        되살린다. 사이드카가 없거나 수량이 어긋나면 그 값들은 포기하고(nan) 크게 경고한다:
        손절 없는 포지션을 조용히 물려받는 게 제일 위험하다.
        """
        from .backtest import _Position
        pos = self.ex.sync_position()            # 실패하면 예외 → 기동 중단(모르는 채 매매 금지)
        saved = self.ex.load_saved_position()
        if pos is None:
            if saved:
                notify("⚠️ 재시작: 거래소는 무포지션인데 로컬엔 포지션 기록이 있음 — "
                       "봇이 멈춘 사이 강제청산/수동청산된 것으로 보고 기록을 정리합니다.")
                print("  [동기화] 거래소 무포지션 → 로컬 포지션 기록 폐기", flush=True)
            self.ex.position = None
            self.ex._save_position()
            return
        same = (saved.get("side") == pos["side"] and saved.get("qty")
                and abs(saved["qty"] - pos["qty"]) <= pos["qty"] * 0.02)
        signal = resample(base, self.tf_min)
        entry_time = int(saved.get("entryTime") or 0) if same else 0
        entry_time = entry_time or int(base.open_time[-1])
        sb = max(0, int(np.searchsorted(signal.open_time, entry_time, side="right")) - 1)
        nan = float("nan")
        p = _Position(
            side=pos["side"], entry_time=entry_time, entry_price=pos["entry_price"],
            qty=pos["qty"], leverage=pos["leverage"],
            margin=pos.get("margin") or pos["entry_price"] * pos["qty"] / max(1, pos["leverage"]),
            liq_price=pos["liq_price"], entry_signal_idx=sb,
            stop_price=(saved.get("stop") if same else None) or nan,
            tp_price=(saved.get("tp") if same else None) or nan,
            entry_fee=(saved.get("entryFee") if same else None) or bm.trade_fee(
                pos["entry_price"], pos["qty"], taker=True,
                taker_fee=self.cfg.taker_fee, maker_fee=self.cfg.maker_fee),
            peak=(saved.get("peak") if same else None) or pos["entry_price"])
        self.ex.position = p
        self.ex._save_position()
        side_k = "롱" if p.side > 0 else "숏"
        msg = (f"🔁 재시작: 거래소 포지션 인계 {side_k} {p.qty} @{p.entry_price:.2f} "
               f"x{p.leverage} (손절 {'없음' if np.isnan(p.stop_price) else f'{p.stop_price:.2f}'})")
        if not same:
            msg += " ⚠️ 로컬 기록 불일치 — 손절/익절가를 못 살렸습니다. 대시보드에서 확인하세요."
        notify(msg)
        print(f"  [동기화] {msg}", flush=True)

    def run(self, interval: int = 60, once: bool = False):
        """폴링 루프. once=True면 한 번만. interval초마다 poll_once(now)."""
        self.bootstrap()
        self._write_state()
        # 모드를 알림에 그대로 — '페이퍼'로 고정돼 있으면 실돈 봇이 페이퍼처럼 보고된다.
        # 테스트넷/실돈까지 구분한다(둘 다 mode='live' 라 한 덩어리로 보면 제일 위험한 착각이 생긴다).
        if self.mode != "live":
            tag = "페이퍼"
        else:
            tag = "🧪 실거래(테스트넷)" if getattr(self.ex, "testnet", False) else "🔴 실거래(실돈)"
        notify(f"▶️ {tag} 시작 {self.preset.name} {self.preset.symbol} {self.preset.timeframe} 잔고 {self.ex.equity():.0f}")
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
    ap = argparse.ArgumentParser(description="페이퍼/실거래 트레이딩 루프")
    ap.add_argument("preset", help="프리셋 JSON 경로 (presets/saved/... 또는 examples/...)")
    ap.add_argument("--paper", action="store_true", help="페이퍼 트레이딩(기본).")
    ap.add_argument("--live", action="store_true",
                    help="실거래(진짜 주문). BINANCE_TESTNET=1(기본)이면 테스트넷 가짜돈.")
    ap.add_argument("--real-money", action="store_true",
                    help="메인넷(실돈) 확인. BINANCE_TESTNET=0 으로 돌릴 땐 이 플래그가 필수.")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--interval", type=int, default=60, help="폴링 간격(초)")
    ap.add_argument("--once", action="store_true", help="한 번만 폴링하고 종료(테스트용)")
    ap.add_argument("--start-running", action="store_true",
                    help="시작 시 바로 매매 활성. 기본은 안전하게 '멈춤'으로 시작(대시보드에서 재개).")
    args = ap.parse_args()

    # 안전 기본값: 봇은 '멈춤' 상태로 시작 → 대시보드에서 명시적으로 켜야 새 진입 시작.
    # (멈춤은 새 진입만 막음 — 기존 포지션 관리·청산은 계속. --start-running 으로 즉시 활성.)
    # --once 는 테스트용이라 페이퍼에선 바로 돌게 두지만, 실거래는 예외 없이 '멈춤'으로 시작한다
    # (진짜 주문이 나가는 경로에서 '한 번만 돌려보려고' 가 제일 흔한 사고 시나리오).
    if not args.start_running and (not args.once or args.live):
        control.set_service("trader", "paused")
        print("🔒 안전 시작: 매매 '멈춤' 상태 — 대시보드에서 봇을 '재개'해야 새 진입이 시작됩니다.", flush=True)

    preset = load_preset_file(args.preset, validate=True)
    mk, tk = bm.fees_for_symbol(preset.symbol)
    cfg = BacktestConfig(initial_equity=args.equity, maker_fee=mk, taker_fee=tk)
    if args.live:
        ex = LiveExecutor(symbol=preset.symbol, maker_fee=mk, taker_fee=tk)  # 마진자산은 심볼에서
        # 실돈은 '두 번' 명시해야 돈다: BINANCE_TESTNET=0 + --real-money.
        # 환경변수 하나만 잘못 건드려도 가짜돈 봇이 실돈 봇이 되는 걸 막는 이중 잠금.
        if not ex.testnet and not args.real_money:
            raise SystemExit(
                "BINANCE_TESTNET=0 (메인넷=실돈) 입니다. 정말 실돈으로 돌리려면 --real-money 를 "
                "함께 주세요. 테스트넷으로 돌리려면 BINANCE_TESTNET=1.")
        ex.preflight()                            # 헤지모드·마진모드·잔고 점검(문제면 여기서 중단)
    else:
        ex = PaperExecutor(equity=args.equity, maker_fee=mk, taker_fee=tk)
    trader = LiveTrader(preset, ex, cfg, strategy_path=args.preset,
                        mode="live" if args.live else "paper")
    tag = "페이퍼" if not args.live else ("실거래(테스트넷)" if ex.testnet else "실거래★실돈★")
    print(f"[{tag}] {preset.name} · {preset.symbol} {preset.timeframe} "
          f"· 수수료 maker {mk*100:.3f}%/taker {tk*100:.3f}% "
          f"· 잔고 {ex.equity():.2f} (기준 {trader.cfg.initial_equity:.2f})")
    trader.run(interval=args.interval, once=args.once)


if __name__ == "__main__":
    main()
