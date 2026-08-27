"""디스코드 조회 명령의 '표현 레이어' — state.json/원장 dict → 사람이 읽는 텍스트.

discord.py·네트워크에 의존하지 않는 순수 함수만 둔다(engine.discord_bot 이 이걸 감싸 응답).
이렇게 분리해야 봇 토큰 없이도 데이터·포맷을 단위테스트로 고정할 수 있다
(ccxt 를 실거래에서만 부르는 것과 같은 이유 — 무거운/외부 의존성은 얇은 껍데기에만).

입력은 이미 만들어진 dict 다:
- state : trader 가 기록한 data/state.json (engine.live._write_state 스키마)
- stats : engine.ledger.stats() 결과
"""
from __future__ import annotations

import copy
import time

_MODE = {"paper": "📝 페이퍼", "testnet": "🧪 테스트넷(실거래)", "live": "🔴 실돈(실거래)"}


def _n(x, dp: int = 2) -> str:
    """숫자 → 천단위 콤마 + 소수 dp 자리. None/비수치는 '–'."""
    if x is None:
        return "–"
    try:
        return f"{float(x):,.{dp}f}"
    except (TypeError, ValueError):
        return str(x)


def _signed(x, dp: int = 2) -> str:
    """부호를 항상 붙인다(+3.2 / -1.0). 손익·수익률용."""
    if x is None:
        return "–"
    try:
        return f"{float(x):+,.{dp}f}"
    except (TypeError, ValueError):
        return str(x)


def _ago(ms) -> str:
    """updatedAt(ms) → '방금/N분 전/N시간 전'. now 는 호출 시각(표시용이라 순수성 예외)."""
    if not ms:
        return "–"
    sec = max(0, int(time.time() * 1000) - int(ms)) // 1000
    if sec < 60:
        return "방금"
    if sec < 3600:
        return f"{sec // 60}분 전"
    if sec < 86400:
        return f"{sec // 3600}시간 전"
    return f"{sec // 86400}일 전"


def _dur(ms: int) -> str:
    """보유 시간(ms) → 'Nh Nm'."""
    if not ms or ms < 0:
        return "–"
    m = int(ms) // 60000
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def _fmt_time(ms) -> str:
    if not ms:
        return "–"
    return time.strftime("%m-%d %H:%M", time.gmtime(int(ms) / 1000)) + " UTC"


def _pos_line(pos: dict) -> str:
    """포지션 한 줄 요약(방향 @진입가 · 미실현손익). /status 용."""
    side = "롱" if pos.get("side", 0) > 0 else "숏"
    up, upc = pos.get("uPnl"), pos.get("uPnlPct")
    tail = "" if up is None else f" · {_signed(up)} ({_signed(upc)}%)"
    return f"{side} @{_n(pos.get('entryPrice'))} x{pos.get('leverage')}{tail}"


def period_bounds(period: str, now_ms: int):
    """기간 선택값 → (start_ms 또는 None, 라벨). now_ms 를 받아 순수 함수로 유지.

    start_ms=None 은 '필터 없음'(전체) → ledger.stats 가 전 구간을 집계한다.
    """
    p = (period or "all").lower()
    if p in ("today", "오늘"):
        return now_ms - (now_ms % 86_400_000), "오늘"        # 오늘 00:00 UTC
    if p in ("7d", "7일", "week"):
        return now_ms - 7 * 86_400_000, "7일"
    if p in ("30d", "30일", "month"):
        return now_ms - 30 * 86_400_000, "30일"
    return None, "전체"


def status_text(state: dict) -> str:
    """/status — 모드·실행상태·전략·잔고·포지션 한눈에."""
    if not state or state.get("error"):
        return f"⚠️ {state.get('error') if state else '상태 없음'} — 봇이 아직 안 돌았거나 state.json 미생성"
    mode = _MODE.get(state.get("mode"), state.get("mode") or "?")
    run = "⏸ 멈춤(새 진입 차단)" if state.get("paused") else "▶️ 실행중"
    pos = state.get("position")
    lines = [
        f"**{mode}** · {run}",
        f"전략: `{state.get('preset')}` · {state.get('symbol')} {state.get('timeframe')}",
        f"잔고: **{_n(state.get('equity'))}** ({_signed(state.get('returnPct'))}%) · 거래 {state.get('numTrades', 0)}건",
        f"포지션: {_pos_line(pos) if pos else '무포지션'}",
    ]
    if state.get("guardrail"):
        lines.append(f"🛡 가드레일 발동: {state['guardrail']} (새 진입 차단 중)")
    if state.get("pendingStrategy"):
        lines.append(f"⏳ 전략 전환 대기: `{state['pendingStrategy']}` (청산 후 적용)")
    if state.get("pendingNetwork"):
        lines.append(f"⏳ 네트워크 전환 대기: {state['pendingNetwork']} (청산 후 적용)")
    lines.append(f"_업데이트 {_ago(state.get('updatedAt'))}_")
    return "\n".join(lines)


def position_text(state: dict) -> str:
    """/position — 보유 포지션 상세(현재가·미실현손익 포함). 대시보드 패널과 같은 값."""
    if not state or state.get("error"):
        return f"⚠️ {state.get('error') if state else '상태 없음'}"
    pos = state.get("position")
    if not pos:
        return "무포지션 — 현재 들고 있는 포지션이 없습니다."
    side = "롱 LONG" if pos.get("side", 0) > 0 else "숏 SHORT"
    up, upc = pos.get("uPnl"), pos.get("uPnlPct")
    uptxt = "–" if up is None else f"{_signed(up)} ({_signed(upc)}%)"
    hold = _dur(int(state.get("updatedAt", 0)) - int(pos.get("entryTime", 0))) if pos.get("entryTime") else "–"
    rows = [
        f"**현재 포지션** · {state.get('symbol')}",
        f"• 방향: **{side}** x{pos.get('leverage')}",
        f"• 진입가: {_n(pos.get('entryPrice'))}  →  현재가: {_n(pos.get('mark'))}",
        f"• 미실현손익: **{uptxt}**",
        f"• 수량: {_n(pos.get('qty'), 6)}",
        f"• 손절: {_n(pos.get('stop'))} · 익절: {_n(pos.get('tp'))} · 청산가: {_n(pos.get('liq'))}",
        f"• 진입: {_fmt_time(pos.get('entryTime'))} (보유 {hold})",
    ]
    return "\n".join(rows)


def apply_note(has_position: bool) -> str:
    """전략/설정 변경이 언제 적용되는지 안내. 무포지션이면 즉시, 보유 중이면 청산 후(대기)."""
    return ("_무포지션 → 다음 폴에 즉시 적용._" if not has_position
            else "_⚠️ 포지션 보유 중 → 청산 후 적용(대기). 지금 열려 있는 거래엔 영향 없음._")


def effective_config(info: dict) -> dict:
    """bot_config_info(){config, presetDefaults} → 지금 봇이 실제로 쓰는 유효 설정.

    config(봇 설정 오버라이드)가 있으면 그게 이기고, 없으면 presetDefaults(프리셋 값). 자본비중은
    sizing.size.value(type=equityPercent)에 들어 있고, 레버리지/마진은 sizing, maker 설정은 execution.
    """
    cfg = (info or {}).get("config") or {}
    dflt = (info or {}).get("presetDefaults") or {}

    def sect(name):
        merged = dict(dflt.get(name) or {})
        merged.update(cfg.get(name) or {})          # 오버라이드가 프리셋을 덮음(얕은 병합)
        return merged

    sizing, execu = sect("sizing"), sect("execution")
    size = sizing.get("size") or {}
    return {
        "symbol": cfg.get("symbol") or dflt.get("symbol"),
        "leverage": sizing.get("leverage"),
        "marginMode": sizing.get("marginMode"),
        "sizeType": size.get("type"),
        "equityPercent": size.get("value") if size.get("type") == "equityPercent" else None,
        "entryType": execu.get("entryType"),
        "makerTimeoutSeconds": execu.get("makerTimeoutSeconds"),
        "useDynamicLeverage": bool(cfg.get("useDynamicLeverage")),
    }


def info_text(state: dict, info: dict) -> str:
    """/info — 지금 돌고 있는 봇의 유효 설정(전략·심볼·사이징·실행)."""
    e = effective_config(info)
    mode = _MODE.get((state or {}).get("mode"), (state or {}).get("mode") or "?")
    lev = "동적 티어" if e["useDynamicLeverage"] else f"{e['leverage']}x 고정"
    entry = "maker 지정가" if e["entryType"] == "makerLimit" else (e["entryType"] or "시장가")
    lines = [
        f"**봇 정보** · {mode}",
        f"전략: `{(state or {}).get('preset', '?')}`",
        f"심볼/주기: {e['symbol']} · {(state or {}).get('timeframe', '?')}",
        f"레버리지: **{lev}** · 마진: {e['marginMode'] or '?'}",
        f"자본비중: {_n(e['equityPercent'], 1)}% (진입당 명목 = 잔고×비중×레버리지)",
        f"진입방식: {entry} · maker 타임아웃 {_n(e['makerTimeoutSeconds'], 0)}s",
    ]
    return "\n".join(lines)


def strategy_confirm_text(choice: dict, has_position: bool) -> str:
    """/strategy — 고른 전략으로 바꾸기 전 확인 요약."""
    return "\n".join([
        f"**전략 전환 확인**",
        f"→ `{choice.get('name')}`  ({choice.get('symbol')} {choice.get('timeframe')})",
        apply_note(has_position),
    ])


def parse_config_form(raw: dict):
    """모달 입력(문자열) → (edits, errors). 빈 칸은 '변경 안 함'(스킵). 실돈이라 범위 검증을 엄격히.

    edits 키: symbol / leverage / equityPercent / makerTimeoutSeconds (준 것만).
    """
    edits, errors = {}, []
    sym = (raw.get("symbol") or "").strip()
    if sym:
        clean = "".join(c for c in sym.upper() if c.isalnum())
        if clean:
            edits["symbol"] = clean
        else:
            errors.append("심볼 형식 오류")
    lev = (raw.get("leverage") or "").strip()
    if lev:
        try:
            v = int(lev)
            if 1 <= v <= 125:
                edits["leverage"] = v
            else:
                errors.append("레버리지는 1~125")
        except ValueError:
            errors.append("레버리지는 정수")
    eq = (raw.get("equity_percent") or "").strip()
    if eq:
        try:
            v = float(eq)
            if 0 < v <= 100:
                edits["equityPercent"] = v
            else:
                errors.append("자본비중은 0 초과 100 이하")
        except ValueError:
            errors.append("자본비중은 숫자")
    to = (raw.get("maker_timeout") or "").strip()
    if to:
        try:
            v = float(to)
            if v >= 0:
                edits["makerTimeoutSeconds"] = v
            else:
                errors.append("maker 타임아웃은 0 이상")
        except ValueError:
            errors.append("maker 타임아웃은 숫자")
    return edits, errors


def apply_config_edits(current: dict, edits: dict) -> dict:
    """현재 bot_config 에 edits 를 얹은 새 bot_config. set_bot_config 가 통째로 교체하므로
    **기존 키(useDynamicLeverage·filter·나머지 sizing/execution)를 보존**하려고 깊은 병합한다."""
    cfg = copy.deepcopy(current or {})
    if "symbol" in edits:
        cfg["symbol"] = edits["symbol"]
    if "leverage" in edits or "equityPercent" in edits:
        s = dict(cfg.get("sizing") or {})
        if "leverage" in edits:
            s["leverage"] = edits["leverage"]
        if "equityPercent" in edits:
            s["size"] = {"type": "equityPercent", "value": edits["equityPercent"]}
        cfg["sizing"] = s
    if "makerTimeoutSeconds" in edits:
        e = dict(cfg.get("execution") or {})
        e["makerTimeoutSeconds"] = edits["makerTimeoutSeconds"]
        cfg["execution"] = e
    return cfg


_EDIT_LABEL = {"symbol": "심볼", "leverage": "레버리지", "equityPercent": "자본비중%",
               "makerTimeoutSeconds": "maker 타임아웃(s)"}


def config_confirm_text(edits: dict, has_position: bool) -> str:
    """/config — 바꿀 값 요약 + 적용 시점 안내(확인 전)."""
    rows = [f"• {_EDIT_LABEL.get(k, k)}: **{edits[k]}**" for k in edits]
    return "\n".join(["**봇 설정 변경 확인**", *rows, apply_note(has_position)])


_REASON = {"take_profit": "익절", "stop_loss": "손절", "trailing": "트레일링", "signal": "신호청산",
           "time": "시간청산", "supertrend": "ST전환", "liquidation": "강제청산", "external": "외부청산"}


def _trade_line(r: dict) -> str:
    """원장 한 행 → 한 줄 요약(방향 진입→청산 손익 (사유))."""
    side = "롱" if r.get("side", 0) > 0 else "숏"
    reason = _REASON.get(r.get("reason"), r.get("reason") or "")
    return f"{side} {_n(r.get('entry_price'))}→{_n(r.get('exit_price'))} {_signed(r.get('pnl'))} ({reason})"


def daily_digest_text(rows: list, stats: dict, state: dict, hours: int = 24) -> str:
    """정기 요약 — 지난 hours 시간 청산된 거래 정리. rows=ledger.load(구간), stats=ledger.stats(구간).

    청산이 없으면 짧게 '거래 없음 + 현재 잔고/포지션'. 있으면 집계 + 최고/최저 + 거래 목록(상한).
    """
    head = f"📅 **일일 요약 · 지난 {hours}시간**"
    eq = _n((state or {}).get("equity"))
    pos = (state or {}).get("position")
    posline = _pos_line(pos) if pos else "무포지션"
    n = (stats or {}).get("n", 0)
    if n == 0:
        return "\n".join([head, "청산된 거래 없음.", f"현재: 잔고 {eq} · {posline}"])
    pnls = [r.get("pnl") or 0 for r in rows]
    lines = [
        head,
        f"거래 **{n}건** · 승률 {_n(stats.get('winRate'), 1)}% ({stats.get('wins', 0)}승) "
        f"· 누적 **{_signed(stats.get('totalPnl'))}**",
        f"손익비 PF {_n(stats.get('profitFactor'))} · MDD {_n(stats.get('maxDrawdown'))} "
        f"· 최고 {_signed(max(pnls))} / 최저 {_signed(min(pnls))}",
        "— 거래 —",
    ]
    cap = 15
    for r in rows[-cap:]:                          # 오래된→최신, 최근 cap 건만
        lines.append("• " + _trade_line(r))
    if len(rows) > cap:
        lines.append(f"…외 {len(rows) - cap}건")
    lines.append(f"현재: 잔고 {eq} · {posline}")
    return "\n".join(lines)


def collect_text(rows: list, paused: bool = False, db_bytes=None, stale_min: float = 10) -> str:
    """/collect — 수집기 상태 + 심볼별 캐시 현황.

    '마지막 봉이 몇 분 전이냐'가 핵심이다. 프로세스가 떠 있어도 봉이 안 쌓이면 죽은 것과
    같으므로, 임계를 넘긴 심볼엔 ⚠️ 를 붙여 한눈에 보이게 한다.
    """
    head = f"📊 **캔들 수집** · {'⏸ 멈춤' if paused else '▶️ 수집중'}"
    if db_bytes:
        head += f" · 캐시 {db_bytes / 1e6:.1f} MB"
    if not rows:
        return head + "\n캐시가 비어 있습니다 — 수집기가 아직 안 돌았거나 candles.db 유실."
    lines = [head]
    for r in rows:
        gap = r.get("gap_min")
        mark = "⚠️ " if (gap is not None and gap > stale_min) else ""
        lines.append(f"{mark}`{r['symbol']}` 마지막 봉 {_ago(r['last_ms'])} · {r['count']:,}개 · "
                     f"{_fmt_time(r['first_ms'])} ~ {_fmt_time(r['last_ms'])}")
    return "\n".join(lines)


def control_text(paused: bool, has_position: bool) -> str:
    """/control — 봇 시작/정지 패널. 버튼 아래 붙는 안내 텍스트.

    정지는 '우아한 정지'(control.py) — 새 진입만 막고 보유 포지션은 자연 청산까지 계속 관리한다.
    강제청산이 아니므로 포지션을 들고 있어도 안전하게 누를 수 있다.
    """
    state = "⏸ 정지됨 (새 진입 차단 중)" if paused else "▶️ 실행중"
    lines = [
        f"**봇 제어** · 현재: {state}",
        "• ▶️ **시작** — 새 진입 재개",
        "• ⏸ **정지** — 새 진입 차단 (graceful: 보유 포지션은 계속 관리·청산, 강제청산 안 함)",
    ]
    if has_position and not paused:
        lines.append("_보유 포지션 있음 — 정지해도 이 포지션은 자연 청산까지 관리됩니다._")
    return "\n".join(lines)


def stats_text(stats: dict, label: str = "전체") -> str:
    """/stats — 원장 집계(승률·손익비·MDD). label 은 기간 표기('오늘'/'7일'/'전체')."""
    if not stats or stats.get("n", 0) == 0:
        return f"[{label}] 아직 청산된 거래가 없습니다."
    lines = [
        f"**성과 [{label}]**",
        f"• 거래: {stats['n']}건 · 승률: **{_n(stats.get('winRate'), 1)}%** ({stats.get('wins', 0)}승)",
        f"• 누적손익: **{_signed(stats.get('totalPnl'))}** · 평균: {_signed(stats.get('avgPnl'))}",
        f"• 손익비(PF): {_n(stats.get('profitFactor'))} · 최대낙폭(MDD): {_n(stats.get('maxDrawdown'))}",
    ]
    by = [s for s in (stats.get("byStrategy") or []) if s.get("n")]
    if len(by) > 1:                       # 전략이 둘 이상일 때만 분해해서 보여준다
        lines.append("— 전략별 —")
        for s in by:
            lines.append(f"• `{s['strategy']}`: {s['n']}건 · 승률 {_n(s.get('winRate'), 1)}% "
                         f"· 손익 {_signed(s.get('totalPnl'))}")
    return "\n".join(lines)
