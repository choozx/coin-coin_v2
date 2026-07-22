"""페이퍼 원장 감사 — 백테스트 재현 대조 + 지정가 체결 현실성 측정.

왜: 백테스트는 지정가(makerLimit)도 **신호봉 종가에 무조건 체결**됐다고 가정한다(낙관적).
실거래에선 (a) 가격이 불리하게 올 때만 체결되거나 (b) 신호 방향으로 튀면 미체결이다.
그 낙관 편향이 '얼마인지'를 페이퍼 원장 + 실제 캔들로 추정한다.

두 가지를 본다:
  1) 재현 대조 — 원장과 같은 기간·프리셋으로 백테스트를 돌려 거래를 맞춰본다.
     판정 로직은 이제 Stepper 한 곳이라 원칙적으로 일치해야 한다. 어긋나면 그건 전략이
     아니라 운영 쪽 원인이다(데이터 갭, 봇 재시작, 프리셋/봇설정 변경, 멈춤 구간).
  2) 체결 감사 — 각 지정가 주문이 '실제로 체결됐을까'를 캔들로 검사한다. 단정할 수 없으므로
     **두 기준으로 범위**를 낸다: 가격이 지정가에 닿기만 해도 체결로 보는 낙관(touch)과,
     관통해야 체결로 보는 엄격(through). 실제 체결률은 그 사이에 있다.

한계(정직하게):
  - 1분봉 해상도라 "3초 내 미체결이면 taker" 같은 초 단위 정책은 검증 못 한다.
    최소 단위가 '다음 1분봉 안에 그 가격을 지나갔는가'다.
  - 호가 대기열(queue) 위치를 캔들로는 알 수 없다. 그래서 하나의 수치가 아니라 범위를 낸다.
  - 범위를 좁히려면 실제 체결 로그(주문ID·체결시각·체결가)가 필요하고, 그건 실거래에서만 나온다.

사용법:
    python3 tools/fill_audit.py                                  # data/trades.db, mode=paper
    python3 tools/fill_audit.py --db /path/trades.db --preset presets/examples/live-strategy.json
    python3 tools/fill_audit.py --timeout 3                      # 미체결 판정 대기(분)

EC2 원장을 로컬로 받아서 보려면:
    scp -i ~/.ssh/key.pem ec2-user@<EIP>:~/auto_trading/data/trades.db /tmp/trades.db
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import candle_store, ledger                      # noqa: E402
from engine.backtest import BacktestConfig, run              # noqa: E402
from engine import binance_math as bm                        # noqa: E402
from engine.preset import load_preset_file                   # noqa: E402

MINUTE_MS = 60_000


def _fmt_ts(ms):
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%m-%d %H:%M")


def _load_rows(db, mode, strategy):
    rows = ledger.load(db, mode=mode, strategy=strategy)
    if not rows:
        raise SystemExit(
            f"원장이 비었다: {db} (mode={mode}). 페이퍼 봇을 돌린 뒤 다시 실행하거나 "
            f"--db 로 EC2에서 받아온 파일을 지정할 것.")
    return rows


def _candles(symbol, start_ms, end_ms):
    """감사 구간 캔들. 없으면 바이낸스에서 받아 캐시에 채운다(공개 API, 키 불필요)."""
    pad = 120 * MINUTE_MS                     # 체결 대기 창을 위한 뒤쪽 여유
    return candle_store.ensure(symbol, start_ms - pad, end_ms + pad, verbose=False)


def _would_fill(base, price, buy: bool, t0_ms: int, timeout_min: int):
    """지정가가 timeout_min 분 안에 체결됐을까. 두 기준을 함께 계산한다.

    touch  (낙관): 가격이 지정가에 **닿기만** 해도 체결로 본다 (매수면 low <= P).
                   내 주문이 호가 대기열 맨 앞이었다고 가정하는 셈 → 체결률 상한.
    through(엄격): 가격이 지정가를 **관통**해야 체결로 본다 (매수면 low < P).
                   시장이 내 쪽으로 넘어왔다는 뜻 → 체결률 하한.

    실제 체결률은 이 둘 사이 어딘가다. 대기열 위치는 캔들로 알 수 없으므로 범위로 본다.
    반환: (touch 체결, through 체결, 체결까지 걸린 분 or None, 미체결 중 놓친 최대폭 bp)
    """
    i = int(np.searchsorted(base.open_time, t0_ms, side="left"))
    n = len(base.open_time)
    if i >= n:
        return None, None, None, None          # 감사 구간 밖(캔들 없음)
    touch = through = False
    waited = None
    worst = 0.0
    for k in range(i, min(i + timeout_min, n)):
        lo, hi = float(base.low[k]), float(base.high[k])
        if buy:
            touch = touch or lo <= price
            through = through or lo < price
        else:
            touch = touch or hi >= price
            through = through or hi > price
        if touch and waited is None:
            waited = k - i
        if through:
            break
        # 미체결인 동안 가격이 '신호 방향으로' 달아난 정도(놓친 폭)
        away = (hi - price) if buy else (price - lo)
        worst = max(worst, away / price * 10_000)
    return touch, through, waited, worst


def audit_fills(rows, preset, timeout_min):
    """지정가 주문별 체결 여부 추정. 반환: (진입 감사, 청산 감사)"""
    maker_entry = (preset.data.get("execution") or {}).get("entryType") == "makerLimit"
    symbol = rows[0]["symbol"]
    start = min(r["entry_time"] for r in rows)
    end = max(r["exit_time"] for r in rows)
    base = _candles(symbol, start, end)

    entries, exits = [], []
    for r in rows:
        side = r["side"]
        if maker_entry:
            # 롱 진입 = 매수 지정가, 숏 진입 = 매도 지정가
            touch, through, waited, away = _would_fill(base, r["entry_price"], side == 1,
                                                       r["entry_time"], timeout_min)
            if touch is not None:
                entries.append({**r, "touch": touch, "through": through,
                                "waited": waited, "away_bp": away})
        # maker 로 체결됐다고 가정한 청산: 지정가 익절 + (maker 모드의) SuperTrend 전환
        if r["reason"] in ("take_profit", "supertrend") and (maker_entry or r["reason"] == "take_profit"):
            # 롱 청산 = 매도 지정가, 숏 청산 = 매수 지정가
            touch, through, waited, away = _would_fill(base, r["exit_price"], side == -1,
                                                       r["exit_time"], timeout_min)
            if touch is not None:
                exits.append({**r, "touch": touch, "through": through,
                              "waited": waited, "away_bp": away})
    return entries, exits, maker_entry


def replay_backtest(rows, preset):
    """원장과 같은 구간을 백테스트로 재현. 반환: 감사 구간 안의 백테스트 거래 리스트.

    ⚠️ 워밍업: 지표(SuperTrend·EMA 등)는 과거가 있어야 계산되므로 백테스트 창을 앞으로
    10일 넓힌다. 그 워밍업 구간의 거래는 감사 대상이 아니므로 **시각으로 잘라낸다**
    (안 자르면 없던 차이가 있는 것처럼 보인다).

    비교는 '언제·어떤 사유로' 까지만 의미가 있다. 수량·손익은 잔고 궤적에 따라 갈리는데,
    워밍업 구간 거래가 백테스트 잔고를 먼저 움직여 사이징이 달라지기 때문.
    """
    symbol = rows[0]["symbol"]
    audit_start = min(r["entry_time"] for r in rows)
    start = audit_start - 10 * 24 * 60 * MINUTE_MS
    end = max(r["exit_time"] for r in rows) + MINUTE_MS
    base = candle_store.ensure(symbol, start, end, verbose=False)
    equity0 = rows[0]["equity_after"] - rows[0]["pnl"]
    mk, tk = bm.fees_for_symbol(symbol)
    cfg = BacktestConfig(initial_equity=equity0, maker_fee=mk, taker_fee=tk,
                         funding_schedule=candle_store.funding_schedule(symbol, start, end))
    m = run(base, preset, cfg)
    return [t for t in m.trades if t.entry_time >= audit_start]


def main():
    ap = argparse.ArgumentParser(description="페이퍼 원장 감사 — 백테스트 대조 + 체결 현실성")
    ap.add_argument("--db", default=ledger.LEDGER_PATH, help="원장 파일(기본: data/trades.db)")
    ap.add_argument("--mode", default="paper", choices=["paper", "live"])
    ap.add_argument("--strategy", default=None, help="전략(프리셋 경로)로 필터")
    ap.add_argument("--preset", default=None, help="대조에 쓸 프리셋(기본: 원장의 strategy 값)")
    ap.add_argument("--timeout", type=int, default=3, help="지정가 미체결 판정 대기(분, 기본 3)")
    ap.add_argument("--no-replay", action="store_true", help="백테스트 재현 대조 건너뛰기")
    args = ap.parse_args()

    rows = _load_rows(args.db, args.mode, args.strategy)
    preset_path = args.preset or rows[-1]["strategy"]
    if not os.path.exists(preset_path):
        raise SystemExit(f"프리셋을 찾을 수 없음: {preset_path} — --preset 으로 지정할 것.")
    preset = load_preset_file(preset_path, validate=False)

    sym = rows[0]["symbol"]
    print(f"원장    : {args.db}  mode={args.mode}  {len(rows)}건")
    print(f"전략    : {preset.name}  [{sym} {preset.timeframe}]  ({preset_path})")
    print(f"기간    : {_fmt_ts(rows[0]['entry_time'])} ~ {_fmt_ts(rows[-1]['exit_time'])} UTC")
    paper_pnl = sum(r["pnl"] for r in rows)
    print(f"페이퍼  : 손익 {paper_pnl:+.2f}  (승 {sum(1 for r in rows if r['pnl'] > 0)} / "
          f"패 {sum(1 for r in rows if r['pnl'] <= 0)})")

    # ---- 1) 백테스트 재현 대조 ----
    if not args.no_replay:
        print("\n── 재현 대조 (같은 기간 백테스트) " + "─" * 28)
        try:
            bt = replay_backtest(rows, preset)
            print(f"백테스트: {len(bt)}건 / 페이퍼: {len(rows)}건  (감사 구간 안, 워밍업 제외)")
            # 진입 시각으로 짝짓기 — 수량·손익은 잔고 궤적 차이로 갈릴 수 있어 비교하지 않는다.
            bt_by_entry = {int(t.entry_time): t for t in bt}
            paper_entries = {int(r["entry_time"]) for r in rows}
            matched = paper_entries & set(bt_by_entry)
            only_paper = sorted(paper_entries - set(bt_by_entry))
            only_bt = sorted(set(bt_by_entry) - paper_entries)
            print(f"진입 시각 일치 {len(matched)}건 / 페이퍼에만 {len(only_paper)}건 / "
                  f"백테스트에만 {len(only_bt)}건")
            if not only_paper and not only_bt:
                print("→ 완전 일치. 판정은 같은 코드(Stepper)라 예상된 결과.")
            else:
                print("→ ⚠️ 전략이 아니라 운영 쪽 원인일 가능성이 크다 —")
                print("   봇 멈춤 구간 / 재시작(부트스트랩은 플랫으로 시작) / 데이터 갭 /")
                print("   프리셋·봇설정 변경 이력을 확인할 것.")
                for label, ts in (("페이퍼에만", only_paper[:5]), ("백테스트에만", only_bt[:5])):
                    if ts:
                        print(f"   {label}: " + ", ".join(_fmt_ts(t) for t in ts)
                              + (" …" if len(ts) == 5 else ""))
        except Exception as e:
            print(f"재현 실패: {type(e).__name__}: {e}")

    # ---- 2) 체결 현실성 감사 ----
    print(f"\n── 지정가 체결 감사 (대기 {args.timeout}분, fill-if-touched 근사) " + "─" * 8)
    entries, exits, maker_entry = audit_fills(rows, preset, args.timeout)
    if not maker_entry:
        print("진입이 taker(시장가) 설정 — 진입 체결은 항상 성사. 미체결 리스크는 청산만 해당.")

    for label, items in (("진입", entries), ("maker 청산", exits)):
        if not items:
            continue
        n_touch = sum(1 for x in items if x["touch"])
        n_through = sum(1 for x in items if x["through"])
        print(f"\n[{label}] {len(items)}건")
        print(f"  체결 추정: {n_through}~{n_touch}건 "
              f"({100*n_through/len(items):.0f}~{100*n_touch/len(items):.0f}%) "
              f"— 관통 기준 ~ 터치 기준")
        miss = [x for x in items if not x["touch"]]          # 터치조차 안 된 = 확실한 미체결
        maybe = [x for x in items if x["touch"] and not x["through"]]
        if miss:
            lost = sum(x["pnl"] for x in miss)
            print(f"  ✗ 확실한 미체결 {len(miss)}건 (가격이 닿지도 않음) — 원장 손익 {lost:+.2f}")
            for x in sorted(miss, key=lambda y: -(y["away_bp"] or 0))[:3]:
                print(f"     {_fmt_ts(x['entry_time'])} {'롱' if x['side']==1 else '숏'} "
                      f"@{x['entry_price']:.2f} 놓친폭 {x['away_bp']:.1f}bp pnl {x['pnl']:+.2f}")
        if maybe:
            print(f"  ? 경계 {len(maybe)}건 (가격이 닿았지만 관통 안 함 — 대기열 앞이었어야 체결) "
                  f"원장 손익 {sum(x['pnl'] for x in maybe):+.2f}")
        waits = [x["waited"] for x in items if x["touch"] and x["waited"] is not None]
        if waits:
            print(f"  체결까지: 즉시(같은 분) {waits.count(0)}건 / 1분+ {len([w for w in waits if w])}건")

    if entries:
        opt = sum(x["pnl"] for x in entries if not x["touch"])            # 확실한 미체결만 제외
        pes = sum(x["pnl"] for x in entries if not x["through"])          # 경계까지 미체결로 간주
        print(f"\n결론: 진입 미체결분을 빼면 손익 {paper_pnl:+.2f} → "
              f"{paper_pnl - opt:+.2f} (터치 기준) ~ {paper_pnl - pes:+.2f} (관통 기준)")
        print("이 범위가 '지정가 체결을 낙관적으로 가정한 대가'다. 실거래 체결은 이 사이에 떨어진다 —")
        print("좁히려면 실제 체결 로그(주문ID·체결시각·체결가)가 필요하고, 그건 실거래를 켜야 나온다.")


if __name__ == "__main__":
    main()
