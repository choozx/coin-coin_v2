"""실거래/페이퍼 트레이딩 루프 — 백테스트와 '같은 판정 로직', 실시간 클럭.

설계(이 프로젝트 대전제): 전략 엔진(지표·조건·사이징·청산·수수료)은 backtest.py의 것을
그대로 import해 재사용하고, 주문 실행만 Executor(페이퍼/실거래)로 갈아끼운다.

흐름:
  1) 1분봉을 주기 폴링(candle_store) → 최신 '닫힌' 봉까지 확보
  2) 상위 TF 리샘플 → 봉마다 backtest.Stepper.step() 호출
  3) 결정은 Executor로 실행 (PaperExecutor=시뮬 / LiveExecutor=ccxt 실주문 —
     binance_broker 가 post-only 지정가 → N초 → 시장가로 체결하고 실체결가·수수료를 되받는다)

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

import numpy as np


from .notifier import notify, routing_summary   # 알림 전송은 engine.notifier(트레이더·워치독 공용).

from . import binance_math as bm
from . import candle_store
from . import control
from . import entry_log
from . import settings
from . import ledger
from . import indicators as ind
from .candles import resample, signal_close_index, TIMEFRAME_MINUTES, MINUTE_MS
from .conditions import (SeriesResolver, collect_operands, operand_label,
                         required_warmup_bars)
from .preset import Preset, load_preset_file, merge_bot_config
from .executor import PaperExecutor, LiveExecutor
from .backtest import BacktestConfig, Stepper
from .binance_broker import RateLimited


class LiveTrader:
    """실시간 봉 스트림(폴링)을 백테스트와 같은 로직으로 처리해 Executor에 주문."""

    # 상태 스냅샷 경로는 대시보드와 같은 env 를 본다 — 여기만 하드코딩이라 STATE_PATH 를 옮기면
    # 대시보드는 새 경로를, 트레이더는 옛 경로를 써서 화면이 조용히 안 갱신됐다.
    def __init__(self, preset: Preset, executor, cfg: BacktestConfig = None, warmup_days: float = 10,
                 state_path: str = None, strategy_path: str = None,
                 mode: str = "paper", ledger_path: str = None):
        state_path = state_path or os.environ.get("STATE_PATH") or "data/state.json"
        self.preset = preset
        self.ex = executor
        self.cfg = cfg or BacktestConfig()
        self.warmup_days = warmup_days
        self.state_path = state_path         # 대시보드가 읽을 상태 스냅샷(포지션·트레이드·잔고)
        self.strategy_path = strategy_path   # 현재 활성 전략 파일 경로(대시보드 선택과 비교)
        self.is_live = (mode == "live")      # 진짜 주문을 내는가(페이퍼 아님)
        self.mode = self._ledger_mode()      # 원장 버킷: paper | testnet | live
        self.ledger_path = ledger_path or ledger.LEDGER_PATH
        self._pending_strategy = None        # 전환 대기(포지션 청산 후 적용할 전략)
        self._strategy_error = None
        self._pending_network = None         # 전환 대기(포지션 청산 후 갈아탈 네트워크)
        self._network_error = None
        self._base_data = copy.deepcopy(preset.data)   # 프리셋 원본(신호 소스) — 봇설정과 병합
        self._bot_cfg = {}                   # 마지막 적용한 봇 설정
        self._rebuild_effective()            # 신호(프리셋) + 봇설정(심볼·사이징·실행·필터) 병합
        self._last_ot = None                 # 마지막으로 처리한 1분봉 open_time
        self._last_price = None              # 마지막 닫힌 1분봉 종가(대시보드 현재가·미실현손익)
        self._flat_divergence = 0            # '거래소 무포지션인데 로컬 보유' 연속 관측 수(디바운스)
        self._started_at = int(time.time() * 1000)
        # 멈춤/재개는 control.json 이 진실. 시작 시점의 값을 그대로 읽어둔다 —
        # False 로 시작하면 첫 폴링에서 '멈춤으로 바뀜'을 오탐해 기동 때마다 알림이 한 번 더 간다.
        self._paused = control.service_state("trader") == "paused"
        self._events = []                    # 이번 폴링에서 발생한 이벤트(hook이 채움)
        self._guardrail_reason = None        # 리스크 가드레일 발동 사유(없으면 None)
        self._guardrail_note = None          # 해제 알림에 덧붙일 한 줄(예: 쿨다운 종료 사유)
        self.entry_log_path = os.environ.get("ENTRY_LOG_PATH", entry_log.DEFAULT_PATH)
        self._signal = None                  # 마지막 폴의 신호봉(로그가 봉 시각을 적을 때 참조)
        self._banned_until = 0               # 레이트리밋 밴 만료(ms) — 그때까진 폴을 쉰다
        self._last_entry_check = None        # 마지막 진입 판정 기록(대시보드·로그 요약)
        self._diagnosed_ot = None            # 이번 폴에서 '진짜 판정'을 기록한 신호봉(중복 방지)
        self._restore_from_ledger()          # 원장에서 잔고·이력 복원(재시작해도 안 사라짐)

    def _ledger_mode(self) -> str:
        """원장 버킷 — 페이퍼·테스트넷·실돈은 절대 섞이면 안 된다.

        셋이 한 버킷에 쌓이면 가짜돈 손익이 실돈 수익률에 들어가고, 실거래의 기준잔고 역산
        ('현재 실잔고 − 누적 실현손익')이 통째로 틀어진다.
        """
        if not self.is_live:
            return "paper"
        return "testnet" if getattr(self.ex, "testnet", False) else "live"

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
        # ★ 창 길이를 프리셋에 맞춘다. 10일 고정이면 상위 TF 에서 지표가 영영 NaN 이고
        #   (10일 = 4h 61봉 / 1d 11봉), NaN 은 항상 false 라 **한 번도 진입하지 않는다**.
        #   그런데 조용해서 '조건 미충족'과 구별되지 않는다. 줄이지는 않는다 — 기존 저TF
        #   동작을 바꾸지 않기 위해 기본값과 큰 쪽을 쓴다.
        need_bars = required_warmup_bars(preset.data.get("entry"),
                                         *[r.get("when") for r in (preset.data.get("entryRules") or [])],
                                         (preset.exit or {}).get("condition"))
        need_days = need_bars * self.tf_min / (24 * 60)
        if need_days > self.warmup_days:
            print(f"  [워밍업] {preset.timeframe} · 지표 수렴에 {need_bars}봉 필요 → "
                  f"창 {self.warmup_days:.1f}일 → {need_days:.1f}일로 확장", flush=True)
            self.warmup_days = need_days
        if getattr(self, "stepper", None) is None:
            self.stepper = Stepper(preset, self.cfg, self.ex,
                                   entry_gate=self._entry_gate,
                                   on_open=self._on_open, on_close=self._on_close,
                                   diagnose=self._on_entry_check)
        else:
            self.stepper.apply_preset(preset)      # 쿨다운(last_exit_time)은 유지

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
        # 사이징 하드 상한(글로벌 가드레일) 주입 — 오설정으로 한 방에 계좌 날리는 걸 막는 천장.
        # 어떤 프리셋/봇설정 값이 와도 레버리지·증거금비율이 이 이상 못 나간다(_open_position·_leverage_for).
        gr = settings.get_guardrails()
        self.cfg.max_leverage = int(gr.get("maxLeverage") or self.cfg.max_leverage)
        self.cfg.max_account_fraction = float(gr.get("maxAccountFractionPct", 100.0)) / 100.0
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
            notify(f"⚠️ 전략 전환 실패 {desired}: {e}", category="system")
            print(f"  [전략전환 실패] {e}", flush=True)
            return
        self._apply_strategy(new, desired)

    def _maybe_switch_network(self, base):
        """대시보드가 고른 거래소 네트워크로 갈아탄다 — 무포지션일 때만(전략 전환과 같은 규칙).

        전환은 '다른 계정으로 이사'다: 잔고·포지션·원장이 전부 바뀐다. 그래서 성공하면
        원장 버킷을 갈아끼워 이력·기준잔고를 다시 읽고, 새 계정에 포지션이 남아 있으면 인계받는다.
        실패하면 executor 가 원래 네트워크로 되돌려 놓고, 사유는 대시보드에 띄운다.
        """
        if not self.is_live or not hasattr(self.ex, "set_network"):
            return
        desired = control.get_network()
        if not desired or desired == self.ex.network:
            self._pending_network = None
            return
        if self.ex.position is not None:
            self._pending_network = desired      # 포지션 열림 → 청산 후로 미룸
            return
        old = self.ex.network
        try:
            self.ex.set_network(desired)
        except Exception as e:
            self._network_error = f"{desired}: {e}"
            self._pending_network = desired
            notify(f"⚠️ 네트워크 전환 실패 {old} → {desired}: {e}", category="system")
            print(f"  [네트워크 전환 실패] {e}", flush=True)
            return
        self._pending_network = None
        self._network_error = None
        self.mode = self._ledger_mode()          # 원장 버킷 교체(가짜돈/실돈 분리)
        self.ex.trades = []
        self._restore_from_ledger()              # 새 네트워크의 이력·기준잔고로
        self.stepper.last_exit_time = -10 ** 15     # 쿨다운 리셋(다른 계정이니 이어갈 게 없다)
        self._sync_live_position(base)           # 새 계정에 포지션이 남아 있으면 인계
        tag = "🧪 테스트넷(가짜돈)" if self.ex.testnet else "🔴 메인넷(실돈)"
        notify(f"🔀 네트워크 전환 {old} → {desired} · {tag} · 잔고 {self.ex.equity():.2f}", category="system")
        print(f"  [네트워크 전환] {old} → {desired} · 잔고 {self.ex.equity():.2f}", flush=True)

    def _guardrail_block(self):
        """글로벌 리스크 가드레일 — 걸리면 사유(str), 아니면 None. 새 진입만 막음(청산·관리는 계속)."""
        g = settings.get_guardrails()
        if g.get("killSwitch"):
            return "킬스위치"
        trades = getattr(self.ex, "trades", []) or []
        mcl = g.get("maxConsecutiveLosses") or {}
        if mcl.get("enabled") and mcl.get("count"):
            reason, new_reset = loss_streak_block(
                trades, mcl["count"], float(mcl.get("cooldownHours") or 0),
                settings.get_streak_reset_ms(), int(time.time() * 1000))
            if new_reset is not None:
                settings.set_streak_reset_ms(new_reset)      # 쿨다운 만료 → 회로 재폐쇄
                # 알림은 아래 _check_guardrail 의 '해제' 한 통에 합친다(같은 사건을 두 번 알리지 않는다).
                self._guardrail_note = f"연속 손실 쿨다운 종료 — 여기서부터 다시 {int(mcl['count'])}회를 셉니다."
                print("  [가드레일] 연속 손실 쿨다운 종료 → 기준선 리셋", flush=True)
            if reason:
                return reason
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
                notify(f"🛡 리스크 가드레일 발동 — 새 진입 차단: {gr}", category="trade")
                print(f"  [가드레일] {gr} → 새 진입 차단", flush=True)
            else:
                # 해제도 알린다 — 예전엔 print 만이라 '왜 다시 도나'가 채널에 안 남았다.
                tail = f" ({self._guardrail_note})" if self._guardrail_note else ""
                notify(f"🛡 리스크 가드레일 해제 — 새 진입 재개{tail}", category="trade")
                print("  [가드레일] 해제 → 진입 재개", flush=True)
            self._guardrail_note = None
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
        if self.preset.symbol != old_sym:    # 심볼 바뀌면 기준봉이 의미를 잃는다
            self._skip_to_latest("심볼 변경")   # ★ None 으로 두면 다음 폴이 10일치를 replay 한다
        self.stepper.last_exit_time = -10 ** 15
        s = self.preset.sizing
        notify(f"⚙️ 봇 설정 반영 — {self.preset.symbol} lev{s.get('leverage')}", category="system")
        print(f"  [봇설정 반영] {self.preset.symbol} · lev{s.get('leverage')} · "
              f"{(s.get('size') or {}).get('type')} · maker={self.stepper.maker_entry}", flush=True)

    def _skip_to_latest(self, context: str = "전환") -> None:
        """처리 기준봉을 '지금 최신 닫힌 봉'으로 옮긴다 — 과거 신호를 몰아 실행하지 않도록.

        심볼·전략이 바뀌면 기존 기준점이 의미를 잃는다. 그렇다고 None 으로 두면 다음 폴이
        창 전체를 replay 한다(위 poll_once 주석 참고). 받아오기에 실패하면 **기존 값을 유지**
        한다 — _last_ot 은 시각이라 심볼이 바뀌어도 '그 시각 이후만 처리'라는 의미가 남는다.
        """
        try:
            base = self._fetch(int(time.time() * 1000))
            if len(base):
                self._last_ot = int(base.open_time[-1])
                return
        except Exception as e:
            print(f"  [경고] {context}: 기준봉 세팅 실패({e}) — 이전 기준점 유지", flush=True)

    def _apply_strategy(self, preset: Preset, path: str):
        """무포지션 상태에서 전략을 실제로 갈아끼운다(심볼 바뀌면 수수료·데이터도 갱신)."""
        old = self.preset.name
        self._base_data = copy.deepcopy(preset.data)   # 새 신호 소스
        self.strategy_path = path
        self._rebuild_effective()                      # 신호 교체 + 봇설정 재적용(심볼·수수료 포함)
        self.stepper.last_exit_time = -10 ** 15         # 쿨다운 리셋
        self._pending_strategy = None
        self._strategy_error = None
        self._skip_to_latest("전략 전환")     # 과거 replay 방지: 기준봉을 최신으로
        notify(f"🔄 전략 전환 {old} → {self.preset.name} ({self.preset.symbol} {self.preset.timeframe})", category="system")
        print(f"  [전략전환] {old} → {self.preset.name} ({self.preset.symbol} {self.preset.timeframe})", flush=True)

    def _unrealized(self, pos) -> dict:
        """보유 포지션의 현재가·미실현손익(대시보드). 현재가 = 마지막 닫힌 1분봉 종가.

        uPnl 은 gross(수수료·펀딩 전) — 백테스트 Stepper 가 종가로 평가하는 것과 같은 기준이라
        이력의 실현손익과 미묘하게 다를 수 있다(그건 수수료·펀딩까지 반영). uPnlPct 는 증거금
        대비 수익률(ROE) — 레버리지가 반영된, 레버리지 트레이더가 보는 숫자.
        """
        mark = self._last_price
        if mark is None:
            return {"mark": None, "uPnl": None, "uPnlPct": None}
        gross = pos.side * (float(mark) - float(pos.entry_price)) * float(pos.qty)
        margin = pos.margin or (float(pos.entry_price) * float(pos.qty) / max(1, pos.leverage))
        return {"mark": round(float(mark), 2), "uPnl": round(gross, 2),
                "uPnlPct": round(gross / margin * 100, 2) if margin else None}

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
            "entryCheck": self._last_entry_check,        # 마지막 진입 판정(왜 안 샀나) — entry_log 와 같은 형식
            "bannedUntil": self._banned_until or None,   # 레이트리밋 밴 만료(ms). 이 동안은 폴을 쉰다
            "mode": self.mode,                           # paper | testnet | live (원장 조회용 = 버킷)
            # 네트워크 스위치용. canMainnet=이 프로세스가 실돈 권한(--real-money)을 받았는가.
            "network": getattr(ex, "network", None) if self.is_live else None,
            "canMainnet": bool(getattr(ex, "allow_mainnet", False)) if self.is_live else False,
            "pendingNetwork": self._pending_network,     # 전환 대기(포지션 청산 후) or None
            "networkError": self._network_error,
            "strategy": self.strategy_path,              # 현재 활성 전략 파일 경로
            "pendingStrategy": self._pending_strategy,   # 전환 대기(포지션 청산 후) 경로 or None
            "strategyError": self._strategy_error,
            "equity": round(ex.equity(), 2), "initialEquity": round(self.cfg.initial_equity, 2),
            "returnPct": round((ex.equity() / self.cfg.initial_equity - 1) * 100, 2),
            "numTrades": len(trades),
            "position": None if pos is None else {
                "side": pos.side, "entryPrice": _px(pos.entry_price), "qty": round(pos.qty, 6),
                "leverage": pos.leverage, "stop": _px(pos.stop_price), "tp": _px(pos.tp_price),
                "liq": _px(pos.liq_price), "entryTime": int(pos.entry_time),
                **self._unrealized(pos)},
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
        """한 번 폴링 → 새로 닫힌 1분봉들을 순서대로 처리. 반환: 이번에 발생한 이벤트 리스트.

        base 를 직접 주입하면 '전체 replay 의도'로 본다(백테스트 대조 테스트가 그렇게 쓴다).
        실운영은 우리가 창을 받아오므로, 기준점이 없을 때 과거를 몰아 실행하지 않는다.
        """
        injected = base is not None
        if base is None:
            self._maybe_switch_strategy()    # 폴링 시작 시 전략 전환 확인(무포지션이면 교체)
            self._maybe_apply_bot_config()   # 봇 설정(심볼·사이징·실행·필터) 변경 반영(무포지션이면)
            if now_ms is None:
                raise ValueError("now_ms 필요(실시간). 테스트는 base 주입.")
            base = self._fetch(now_ms)
            # 네트워크 전환은 캔들을 받은 뒤에 — 새 계정의 포지션을 인계받으려면 신호봉이 필요하다.
            self._maybe_switch_network(base)
        if len(base) < 2:
            return []
        self._last_price = float(base.close[len(base) - 1])   # 현재가 = 마지막 닫힌 1분봉 종가
        self._sync_paused()              # 멈춤이면 새 진입 차단 + 바뀐 순간엔 알림
        self._diagnosed_ot = None        # 이번 폴의 '진짜 판정' 기록 여부(미리보기 중복 방지)
        signal = resample(base, self.tf_min)
        self._signal = signal
        bar_of, is_close = signal_close_index(base, self.tf_min)
        atr_series = ind.atr(signal.high, signal.low, signal.close, 14)
        resolver = SeriesResolver(signal)

        self._events = []                # hook(_on_open/_on_close)이 여기에 쌓는다
        self._maybe_flatten(now_ms)      # 사용자 즉시청산 요청 처리(있으면 시장가 청산 + 자동 정지)
        self._reconcile_live_position(base)   # 유령 포지션 위에서 판정하지 않게 스텝 전에 거래소와 맞춘다
        # 아직 처리 안 한 1분봉만 (갭이 있어도 순서대로 따라잡음)
        if self._last_ot is not None:
            start = int(np.searchsorted(base.open_time, self._last_ot, side="right"))
        elif injected:
            start = 0                     # 호출자가 캔들을 직접 준 경우 = 전체 replay 의도
        else:
            # ★ 실운영인데 기준점이 없다 = 기동 직후이거나 전환 경로가 세팅을 빠뜨린 것.
            #   여기서 창 전체를 훑으면 최대 warmup_days(기본 10일=14,400봉) 의 과거 신호가
            #   **지금 시세로** 한꺼번에 체결된다 — 라이브면 실주문 수십 건이다
            #   (실측: 심볼 변경 한 번에 한 폴 7트레이드=14주문).
            #   개별 경로(_skip_to_latest)도 세팅하지만 하나라도 빠뜨리면 돈이 나가므로
            #   판정 직전에 한 번 더 막는다. 최신 봉 하나만 처리하고 기준점을 잡는다.
            start = len(base) - 1
            print("  [경고] 처리 기준봉이 없어 최신 봉만 처리 — 과거 replay 방지", flush=True)
        for t in range(start, len(base)):
            self.stepper.step(base, signal, bar_of, is_close, atr_series, resolver, t)
            self._last_ot = int(base.open_time[t])
        # 신호봉이 안 닫힌 폴에서도 '지금 왜 안 사는가'를 남긴다 — 15m 프리셋이면 진짜 판정은
        # 15분에 한 번뿐이라, 그것만 남기면 그 사이 지표가 어디쯤인지 볼 수 없다.
        self._preview_entry_check(signal, resolver, int(now_ms or time.time() * 1000))
        return self._events

    # ---- per-bar 처리 ----
    # 판정 로직은 backtest.Stepper 한 곳에만 있다(백테스트와 문자 그대로 같은 코드).
    # 여기 남는 건 '결과를 어떻게 기록할지' 뿐 — 이벤트 발행·원장 append·진입 게이트.

    def _sync_paused(self) -> bool:
        """대시보드의 멈춤/재개를 반영. **바뀐 순간에만** 알린다(폴링마다 보내면 스팸).

        가드레일 알림과 같은 방식. '언제 매매를 켜고 껐는지'가 남아야 나중에 로그를 볼 때
        '이 구간은 왜 진입이 없지?' 를 추적할 수 있다.
        """
        paused = control.service_state("trader") == "paused"
        if paused != self._paused:
            self._paused = paused
            if paused:
                notify("⏸ 매매 멈춤 — 새 진입 차단 (보유 포지션 관리·청산은 계속)", category="trade")
                print("  [제어] 멈춤 → 새 진입 차단", flush=True)
            else:
                notify(f"▶️ 매매 재개 — 새 진입 시작 ({self.preset.symbol} {self.preset.timeframe})", category="trade")
                print("  [제어] 재개 → 새 진입 시작", flush=True)
        return paused

    def _entry_gate(self) -> bool:
        """새 진입 허용 여부. 멈춤·리스크 가드레일은 진입만 막고 기존 포지션 관리는 계속."""
        return not self._paused and self._check_guardrail() is None

    def _on_entry_check(self, resolver, sb, fill_time, side, block):
        """Stepper 가 신호봉을 닫으며 부르는 훅 = **진짜 판정**.

        단, 재시작 직후 첫 폴은 받아온 캔들 전체를 처음부터 다시 훑는다(_last_ot 가 없어서).
        그 replay 의 신호봉마다 기록하면 기동할 때마다 수백 줄이 쏟아져 정작 '지금'이 파묻힌다.
        지금 것만 남긴다 — 과거 판정은 이미 원장(체결)이나 백테스트로 볼 수 있다.
        """
        bar_ms = self._signal_open_time(sb)
        if self._is_replay(bar_ms):
            return
        self._record_entry_check(resolver, sb, bar_ms, side, block, decided=True)
        self._diagnosed_ot = bar_ms

    def _is_replay(self, bar_ms: int) -> bool:
        """이 신호봉이 '지금'이 아니라 따라잡기 중인 과거 봉인가(신호봉 2개보다 오래됐는가)."""
        if not bar_ms:
            return False
        return (time.time() * 1000 - bar_ms) > 2 * self.tf_min * 60_000

    def _record_entry_check(self, resolver, sb, bar_ms, side, block, decided: bool):
        """판정 하나를 entry_log 에 남기고 상태 파일용으로도 들고 있는다.

        관찰용이므로 어떤 실패도 매매를 막지 않는다 — 여기서 예외가 올라가면 폴 루프가
        에러로 처리돼 '로그를 못 써서 봇이 선다'가 된다. 그건 본말전도다.
        """
        try:
            rec = entry_log.build(self.preset, self.stepper.entry_rules, resolver, sb,
                                  bar_ms, int(time.time() * 1000), side, block,
                                  self._last_price, decided)
        except Exception as e:
            print(f"  [진입로그 실패] {e}", flush=True)
            return
        self._last_entry_check = rec
        entry_log.append(rec, self.entry_log_path)
        if decided:                          # 진짜 판정일 때만 조건 트리를 통째로 찍는다
            print(f"  [진입판정] {entry_log.summary(rec)}", flush=True)
            for r in rec["rules"]:
                print(f"    ({r['side']})", flush=True)
                for ln in r["lines"]:
                    print(f"      {ln}", flush=True)

    def _signal_open_time(self, sb) -> int:
        """신호봉 인덱스 → open_time(ms). 로그의 'bar' 필드가 어느 봉인지 가리키게."""
        try:
            return int(self._signal.open_time[sb])
        except Exception:
            return 0

    def _preview_entry_check(self, signal, resolver, now_ms):
        """폴링마다 남기는 미리보기 — 아직 안 닫힌 신호봉 위에서 '지금이라면' 을 본다.

        진짜 판정과 **같은 Stepper.entry_block** 을 쓴다. 로그가 판정과 다른 코드로 갈라지면
        로그가 거짓말을 하게 되고, 그러면 없느니만 못하다.
        """
        sb = len(signal) - 1
        if sb < 0:
            return
        if self._diagnosed_ot is not None and self._diagnosed_ot == int(signal.open_time[sb]):
            return                            # 이 봉은 이번 폴에서 진짜 판정으로 이미 남겼다
        try:
            side, block = self.stepper.entry_block(signal, resolver, sb, now_ms)
        except Exception as e:
            print(f"  [진입로그 실패] {e}", flush=True)
            return
        self._record_entry_check(resolver, sb, int(signal.open_time[sb]), side, block, decided=False)

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
        if self.is_live and hasattr(self.ex, "sync_position"):
            self._sync_live_position(base)
        start = "플랫으로" if self.ex.position is None else "포지션 인계받아"
        print(f"부트스트랩: {len(base)}봉 워밍업, {start} 시작 → 이후 새로 닫히는 봉만 실행.", flush=True)
        self._warn_if_indicators_nan(base)

    def _warn_if_indicators_nan(self, base) -> None:
        """기동 시 지표가 NaN 이면 크게 알린다 — 조용히 '진입 조건 미충족'으로 위장되는 상태다.

        창을 프리셋에 맞춰 늘려도 **캐시에 그만큼의 과거가 없으면** 여전히 NaN 이다. 그러면
        봇은 멀쩡히 돌면서 한 번도 진입하지 않고, 로그엔 '조건 미충족'만 쌓인다. 실제로
        10일 고정 창에서 4h/1d 프리셋이 이 상태였다 — 아무도 몰랐을 것이다.
        """
        try:
            if len(base) < 2:
                return
            signal = resample(base, self.tf_min)
            r = SeriesResolver(signal)
            i = len(signal) - 1
            nodes = [self.preset.data.get("entry")]
            nodes += [rule.get("when") for rule in (self.preset.data.get("entryRules") or [])]
            bad = []
            for node in nodes:
                for op in collect_operands(node if isinstance(node, dict) else {}):
                    v = float(r.resolve(op)[i])
                    if np.isnan(v):
                        bad.append(operand_label(op))
            if bad:
                names = ", ".join(sorted(set(bad)))
                msg = (f"⚠️ 지표 워밍업 부족 — {names} 가 NaN 입니다. 이 상태로는 진입 조건이 "
                       f"**절대 참이 되지 않습니다**(신호봉 {len(signal)}개). "
                       f"캐시에 과거 데이터를 더 채우세요(/collector).")
                notify(msg, category="system", buttons=["status"])
                print(f"  [워밍업 경고] {names} NaN · 신호봉 {len(signal)}개", flush=True)
        except Exception as e:
            print(f"  [워밍업 점검 실패] {e}", flush=True)

    def _sync_live_position(self, base, context: str = "재시작"):
        """거래소의 실제 포지션을 진실로 삼아 엔진 상태를 맞춘다(재시작·폴 중 재조정 공통).

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
                       "봇이 멈춘 사이 강제청산/수동청산된 것으로 보고 기록을 정리합니다.", category="trade")
                print("  [동기화] 거래소 무포지션 → 로컬 포지션 기록 폐기", flush=True)
            self.ex.position = None
            self.ex._save_position()
            return
        same = (saved.get("side") == pos["side"] and saved.get("qty")
                and abs(saved["qty"] - pos["qty"]) <= pos["qty"] * 0.02)
        signal = resample(base, self.tf_min)
        entry_time = int(saved.get("entryTime") or 0) if same else 0
        entry_time = entry_time or int(base.open_time[-1])
        # 거래소에서 인계받은 포지션의 '진입 신호봉' — 인덱스가 아니라 **시각**으로 잡는다
        # (배열은 폴마다 미끄러지지만 시각은 안 변한다. timeStop 이 이걸 기준으로 센다.)
        sb = max(0, int(np.searchsorted(signal.open_time, entry_time, side="right")) - 1)
        sb_time = int(signal.open_time[sb]) if len(signal) else int(entry_time)
        nan = float("nan")
        p = _Position(
            side=pos["side"], entry_time=entry_time, entry_price=pos["entry_price"],
            qty=pos["qty"], leverage=pos["leverage"],
            margin=pos.get("margin") or pos["entry_price"] * pos["qty"] / max(1, pos["leverage"]),
            liq_price=pos["liq_price"], entry_signal_time=sb_time,
            stop_price=(saved.get("stop") if same else None) or nan,
            tp_price=(saved.get("tp") if same else None) or nan,
            entry_fee=(saved.get("entryFee") if same else None) or bm.trade_fee(
                pos["entry_price"], pos["qty"], taker=True,
                taker_fee=self.cfg.taker_fee, maker_fee=self.cfg.maker_fee),
            peak=(saved.get("peak") if same else None) or pos["entry_price"])
        self.ex.position = p
        self.ex._save_position()
        side_k = "롱" if p.side > 0 else "숏"
        msg = (f"🔁 {context}: 거래소 포지션 인계 {side_k} {p.qty} @{p.entry_price:.2f} "
               f"x{p.leverage} (손절 {'없음' if np.isnan(p.stop_price) else f'{p.stop_price:.2f}'})")
        if not same:
            msg += " ⚠️ 로컬 기록 불일치 — 손절/익절가를 못 살렸습니다. 대시보드에서 확인하세요."
        notify(msg, category="trade")
        print(f"  [동기화] {msg}", flush=True)

    def _maybe_flatten(self, now_ms):
        """사용자 즉시청산 요청(control.flatten) 처리 — 전략과 무관하게 지금 포지션을 시장가로 닫는다.

        대시보드/디스코드는 별개 프로세스라 청산을 직접 못 부른다 → control.json 플래그로 요청하고
        트레이더가 여기서 소비. 긴급 탈출이므로 청산 후 '자동 정지'(즉시 재진입 방지). 재개는 수동.
        """
        if not control.get_flatten():
            return
        control.clear_flatten()                      # 소비(한 번만 실행)
        pos = self.ex.position
        if pos is None:
            notify("🛑 즉시청산 요청 — 이미 무포지션(할 일 없음)", category="trade")
            return
        from .binance_broker import OrderError
        px = self._last_price or pos.entry_price
        ts = int(now_ms or time.time() * 1000)
        try:
            trade = self.ex.close(float(px), "manual", ts, is_maker=False)   # 시장가 reduceOnly
        except OrderError as e:
            notify(f"⚠️ 즉시청산 실패: {e} — 포지션 유지, 수동 확인 요망", category="trade")
            return
        self._on_close(trade)                        # 원장 기록 + close 이벤트(run 루프가 🔴 알림)
        control.set_service("trader", "paused")      # 긴급 탈출 → 자동 정지(재개는 대시보드/디스코드)
        self._paused = True
        notify("🛑 즉시청산 완료 — 새 진입 자동 정지(재개는 수동)", category="trade")

    def _reconcile_live_position(self, base):
        """폴 루프 중 거래소 실제 포지션과 로컬 상태를 대조해 어긋나면 맞춘다(live 전용).

        재시작 때만 맞추면, 도는 중에 봇 모르게 강제청산·수동청산·수동진입·부분축소가 나도
        엔진이 '유령 포지션' 위에서 판정을 계속한다. 그 갭을 매 폴(스텝 전에) 닫는다.
        조회가 실패하면 이번 폴은 손대지 않는다 — 모르는 채로 지우는 게 제일 위험하다.
        """
        if not self.is_live or not hasattr(self.ex, "sync_position"):
            return
        try:
            xpos = self.ex.sync_position()        # 거래소 진실(None=플랫)
        except Exception as e:
            print(f"  [동기화] 거래소 포지션 조회 실패 — 이번 폴 스킵: {e}", flush=True)
            return
        local = self.ex.position

        # ① 거래소 무포지션 + 로컬 보유 → 봇 모르게 청산됨. 파괴적(원장 기록 + 사이드카 삭제 →
        #    손절 유실)이라 순간 빈 조회에 속지 않게 3폴(≈3분) 연속 + 조치 직전 최종 확인까지 한다.
        if xpos is None and local is not None:
            self._flat_divergence += 1
            if self._flat_divergence < 3:
                print("  [동기화] 거래소 무포지션인데 로컬 보유 — 재확인 대기", flush=True)
                return
            try:
                confirm = self.ex.sync_position()      # 파괴적 조치 직전 최종 확인
            except Exception:
                return                                  # 조회 실패 → 보류(모르는 채 안 지운다)
            if confirm is not None:                     # 되살아남 = 순간 빈 조회였음 → 오판 방지
                self._flat_divergence = 0
                return
            self._flat_divergence = 0
            self._external_close(local, base)
            return
        self._flat_divergence = 0

        # ② 거래소 보유 + 로컬 무포지션 → 추적 안 되던 포지션(수동진입/상태저장 실패) → 인계.
        if xpos is not None and local is None:
            self._sync_live_position(base, context="동기화")
            return

        # ③ 둘 다 보유하지만 방향/수량 불일치 → 거래소를 기준으로 맞춘다.
        if xpos is not None and local is not None:
            if xpos["side"] != local.side:
                notify(f"⚠️ 포지션 방향 불일치(거래소 {xpos['side']} vs 로컬 {local.side}) — 거래소 기준 재인계", category="trade")
                self.ex.position = None            # 방향까지 다르면 통째로 재인계가 안전
                self._sync_live_position(base, context="동기화")
            elif abs(xpos["qty"] - local.qty) > local.qty * 0.02:
                old = local.qty
                local.qty = float(xpos["qty"])     # 부분 축소/증가 반영(방향 동일 → 손절·익절 유지)
                self.ex._save_position()
                notify(f"⚠️ 포지션 수량 조정 {old} → {xpos['qty']} (거래소 기준)", category="trade")
                print(f"  [동기화] 수량 조정 {old} → {xpos['qty']}", flush=True)

    def _external_close(self, pos, base):
        """봇 모르게 사라진 포지션을 '외부 청산(external)'으로 원장에 남기고 로컬 정리.

        실제 체결가를 모르므로 청산가는 마지막 종가로 근사한다 — reason='external' 이 그 사실을
        표시한다. 실거래 잔고는 어차피 거래소에서 읽어 정확하지만, 이력이 비면 '왜 잔고가 바뀌었나'를
        못 쫓으므로 한 건 남긴다(펀딩은 거래소 정산분을 조회해 반영).
        """
        exit_price = float(self._last_price or pos.entry_price)
        exit_time = int(base.open_time[len(base) - 1])
        trade = self.ex._record(pos, exit_price, 0.0, "external", exit_time)
        try:
            ledger.record(trade, symbol=self.preset.symbol,
                          strategy=self.strategy_path or self.preset.name,
                          mode=self.mode, equity_after=self.ex.equity(), db_path=self.ledger_path)
        except Exception as e:
            print(f"  [원장기록 실패] {e}", flush=True)
        side_k = "롱" if pos.side > 0 else "숏"
        notify(f"⚠️ 봇 몰래 청산됨 — 외부 청산으로 기록 ({side_k} @{pos.entry_price:.2f} → ~{exit_price:.2f} "
               f"pnl ~{trade.pnl:+.2f}). 강제청산/수동청산 의심 — 청산가는 근사치.", category="trade")
        print(f"  [동기화] 외부 청산 기록 pnl~{trade.pnl:+.2f} (청산가 근사 {exit_price:.2f})", flush=True)

    def run(self, interval: int = 60, once: bool = False):
        """폴링 루프. once=True면 한 번만. interval초마다 poll_once(now)."""
        self.bootstrap()
        self._write_state()
        # 모드를 알림에 그대로 — '페이퍼'로 고정돼 있으면 실돈 봇이 페이퍼처럼 보고된다.
        # 테스트넷/실돈까지 구분한다(둘 다 mode='live' 라 한 덩어리로 보면 제일 위험한 착각이 생긴다).
        tag = {"paper": "페이퍼", "testnet": "🧪 실거래(테스트넷)"}.get(self.mode, "🔴 실거래(실돈)")
        # 오배선(오타 채널ID)을 기동 즉시 눈으로 확인. 트레이더가 보내는 건 매매·시스템 둘뿐이다.
        print(f"  [알림 라우팅] {routing_summary(('trade', 'system'))}", flush=True)
        notify(f"▶️ {tag} 시작 {self.preset.name} {self.preset.symbol} {self.preset.timeframe} 잔고 {self.ex.equity():.0f}", category="system")
        fails = 0
        banned_notified = False              # 밴 알림은 한 번만(만료 후 리셋)
        while True:
            now = int(time.time() * 1000)
            try:
                events = self.poll_once(now_ms=now)
                self._write_state()
                for e in events:
                    print(f"  [{e['type']}] {e}", flush=True)
                    if e["type"] == "open":
                        notify(f"🟢 진입 {'롱' if e['side']>0 else '숏'} @{e['price']:.2f} x{e['lev']} ({self.preset.symbol})",
                               category="trade", buttons=["pause", "flatten"])
                    elif e["type"] == "close":
                        notify(f"🔴 청산 {e['reason']} @{e['price']:.2f} pnl {e['pnl']:+.2f} 잔고 {self.ex.equity():.0f}", category="trade")
                st = f"잔고 {self.ex.equity():.2f}"
                pos = self.ex.position
                st += f" | 포지션 {'롱' if pos.side>0 else '숏'} @{pos.entry_price:.2f}" if pos else " | 무포지션"
                if self._last_entry_check:
                    st += f" | {entry_log.summary(self._last_entry_check)}"
                print(f"{time.strftime('%H:%M:%S')}  {st}", flush=True)
                fails = 0
                if self._banned_until:       # 정상 폴이 돌았다 = 밴 해제
                    notify("✅ 레이트리밋 밴 해제 — 매매를 재개합니다.", category="system")
                    print("  [밴] 해제 — 정상 복귀", flush=True)
                self._banned_until, banned_notified = 0, False
            except RateLimited as e:
                # ★ 밴 중에 계속 때리면 바이낸스가 밴을 **연장한다.** 만료까지 쉬는 게 유일한 대응이다.
                #   실측 사고: -1003 밴 → 청산 재시도가 매 폴 79요청씩 누적 → 밴 연장 → 11분 무응답.
                self._banned_until = e.until_ms
                self._write_state()          # 대시보드가 '왜 멈춰 있나'를 볼 수 있게 남긴다
                wait = max(1.0, e.until_ms / 1000.0 - time.time())
                if not banned_notified:
                    banned_notified = True
                    mins = wait / 60.0
                    notify(f"🚫 레이트리밋 밴 — 바이낸스가 IP 를 차단했습니다. "
                           f"{mins:.1f}분 쉬었다 재개합니다(그동안 포지션 관리가 멈춥니다).",
                           category="system", buttons=["status"])
                print(f"  [밴] 요청 금지 — {wait:.0f}초 대기", flush=True)
                if once:
                    break
                time.sleep(min(wait, 900))   # 15분씩 끊어 자며 만료를 다시 확인
                continue
            except Exception as e:
                fails += 1
                print(f"  [에러] {e}", flush=True)
                if fails in (1, 5, 20):      # 반복 실패 시 알림(스팸 방지)
                    notify(f"⚠️ 에러({fails}회): {e}", category="system", buttons=["status"])
            if once:
                break
            time.sleep(interval)


def loss_streak_block(trades, count: int, cooldown_hours: float, reset_ms: int, now_ms: int):
    """연속 손실 가드레일 판정 → (차단 사유|None, 새 기준선|None).

    두 번째 값이 None 이 아니면 호출자가 그걸 저장해야 한다(쿨다운 만료 = 회로 재폐쇄).

    ★ 왜 기준선이 필요한가: 이 가드레일은 원래 **자기 해제가 불가능한 래치**였다. 발동하면
    새 진입이 막히고 → 새 트레이드가 안 생기고 → 연속 손실 기록이 영원히 그대로라 사람이
    설정을 끄기 전까지 봇이 영구 정지했다. 기준선을 올려 '여기서부터 다시 센다'로 끊는다.

    cooldown_hours=0 은 옛 동작(수동 해제 전까지 유지)이지만, 이제 사유에 그 사실이 적히고
    설정 화면에 해제 버튼이 있다 — 모르는 채 갇히지는 않는다.
    """
    streak, last_loss_ms = 0, None
    for tr in reversed(trades or []):
        et = int(tr.exit_time or 0)
        if et <= reset_ms:               # 기준선 이전(또는 청산시각 불명) → 여기서 끊는다
            break
        if tr.pnl < 0:
            streak += 1
            if last_loss_ms is None:
                last_loss_ms = et        # reversed 이므로 첫 손실 = 가장 최근 손실
        else:
            break
    if streak < int(count):
        return None, None
    if cooldown_hours and last_loss_ms:
        end_ms = last_loss_ms + float(cooldown_hours) * 3_600_000.0
        if now_ms >= end_ms:
            return None, now_ms          # 쿨다운 끝 → 기준선을 지금으로(다시 count 번 져야 발동)
        # ★ 남은 시간이 아니라 '해제 시각'으로 적는다: 사유 문자열이 폴링마다 바뀌면
        #   _check_guardrail 이 매번 상태 변화로 보고 60초마다 알림을 쏜다.
        from datetime import datetime, timezone
        until = datetime.fromtimestamp(end_ms / 1000, timezone.utc).strftime("%m-%d %H:%M UTC")
        return f"연속 손실 {streak}회 (쿨다운 해제 {until})", None
    return f"연속 손실 {streak}회 (자동 해제 없음 — 설정에서 해제)", None


def safety_pause_on_start(start_running: bool, once: bool, live: bool, real_money: bool,
                          path: str = control.DEFAULT_PATH) -> str:
    """기동 시 매매 상태를 정한다. 반환: skip | resumed | kept | overwrote.

    안전 기본값 — 봇은 '멈춤'으로 시작하고 대시보드에서 명시적으로 켜야 새 진입을 낸다.
    (멈춤은 새 진입만 막음: 기존 포지션 관리·청산은 계속. --start-running 으로 즉시 활성.)
    --once 는 테스트용이라 페이퍼에선 바로 돌게 두지만, 실거래는 예외 없이 '멈춤'으로 시작한다
    (진짜 주문이 나가는 경로에서 '한 번만 돌려보려고' 가 제일 흔한 사고 시나리오).

    ★ 단, 그 안전장치가 재배포·OOM·재부팅마다 사용자의 '재개'를 조용히 취소해 봇이 며칠간
    매매를 안 하는 사고를 냈다. 그래서 **가짜돈(페이퍼·테스트넷)에 한해** 사용자 의도를
    이어받아 자동 재개한다. 실돈을 만질 수 있는 프로세스(--real-money)는 예외 없이 멈춤이다 —
    나쁜 배포가 즉시 실주문을 내는 것보다 수동 재개 한 번이 싸다.

    가짜돈 판정은 `real_money` 로 한다(ex.testnet 이 아니라): 이 시점엔 executor 가 아직 없고,
    --real-money 없이는 메인넷에 붙을 수 없으며 대시보드의 네트워크 스위치도 그 권한 안에서만
    움직인다. 즉 real_money=False 는 '이 프로세스는 절대 실돈이 못 된다'는 뜻이라 안전하다.
    """
    if start_running or (once and not live):
        return "skip"
    if not real_money and control.trader_intent(path) == "running":
        control.set_service("trader", "running", path, record_intent=False)
        return "resumed"
    # '덮어썼다'는 이번 기동이 실제로 running → paused 로 바꿨을 때만이다. service_state 는
    # 기록이 없으면 running 을 기본값으로 주므로(최초 기동) 명시 기록만 본다 — 안 그러면
    # 첫 기동마다 헛경고가 나가고, 크래시 루프에선 재시작마다 같은 경고가 반복된다.
    was_running = control.read_control(path).get("trader") == "running"
    control.set_service("trader", "paused", path, record_intent=False)
    return "overwrote" if was_running else "kept"


def main():
    from .env import load_dotenv
    load_dotenv()                            # .env → 환경변수(BINANCE_API_KEY 등)
    ap = argparse.ArgumentParser(description="페이퍼/실거래 트레이딩 루프")
    ap.add_argument("preset", help="프리셋 JSON 경로 (presets/saved/... 또는 examples/...)")
    ap.add_argument("--paper", action="store_true", help="페이퍼 트레이딩(기본).")
    ap.add_argument("--live", action="store_true",
                    help="실거래(진짜 주문). BINANCE_TESTNET=1(기본)이면 테스트넷 가짜돈.")
    ap.add_argument("--real-money", action="store_true",
                    help="이 프로세스에 실돈(메인넷) 권한을 준다. BINANCE_TESTNET=0 으로 돌릴 땐 필수이고, "
                         "대시보드의 테스트넷↔실돈 스위치도 이 권한이 있어야 실돈 쪽으로 움직인다.")
    ap.add_argument("--equity", type=float, default=10_000.0)
    ap.add_argument("--interval", type=int, default=60, help="폴링 간격(초)")
    ap.add_argument("--once", action="store_true", help="한 번만 폴링하고 종료(테스트용)")
    ap.add_argument("--start-running", action="store_true",
                    help="시작 시 바로 매매 활성. 기본은 안전하게 '멈춤'으로 시작(대시보드에서 재개).")
    args = ap.parse_args()

    start = safety_pause_on_start(args.start_running, args.once, args.live, args.real_money)
    if start in ("kept", "overwrote"):
        print("🔒 안전 시작: 매매 '멈춤' 상태 — 대시보드에서 봇을 '재개'해야 새 진입이 시작됩니다.", flush=True)
    if start == "overwrote":
        # 돌고 있던 봇이 다시 뜨면 여기서 사용자의 '재개'가 취소된다. 봇 입장에선 '처음부터 멈춤'이라
        # _sync_paused 의 상태 전이가 없어 알림이 안 나가고, 워치독은 state.json 갱신만 보므로
        # 살아있는 멈춤 봇을 정상으로 판정한다 — 삼중 침묵. 덮어쓸 때는 반드시 알린다.
        notify("⚠️ 봇이 재시작되어 매매가 '멈춤'으로 되돌아갔습니다 "
               "(재배포·재부팅 등). 실돈 봇은 자동 재개하지 않습니다 — 재개해 주세요.",
               category="trade", buttons=["resume", "status"])
    elif start == "resumed":
        print("🔄 의도 보존: 재시작 전 '재개' 상태를 이어갑니다(가짜돈).", flush=True)
        notify("🔄 재시작 후 매매 자동 재개 — 가짜돈(페이퍼·테스트넷)이라 이전 '재개' 상태를 이어갑니다.",
               category="trade", buttons=["pause", "status"])

    preset = load_preset_file(args.preset, validate=True)
    mk, tk = bm.fees_for_symbol(preset.symbol)
    cfg = BacktestConfig(initial_equity=args.equity, maker_fee=mk, taker_fee=tk)
    if args.live:
        # allow_mainnet: 이 프로세스가 실돈을 만질 수 있는가. 대시보드의 네트워크 스위치도
        # 이 권한 안에서만 움직인다 — 버튼 하나로 가짜돈 봇이 실돈 봇이 되면 안 된다.
        ex = LiveExecutor(symbol=preset.symbol, maker_fee=mk, taker_fee=tk,
                          allow_mainnet=args.real_money)       # 마진자산은 심볼에서
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
