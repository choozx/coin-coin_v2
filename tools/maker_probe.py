"""maker 체결 프로브 — 전략 신호를 기다리지 않고 **체결 정책만** 실측한다.

왜 필요한가: 2026-09 에 maker 비율이 2.5% 라는 게 드러났다(엔진은 maker 를 가정하는데
실제로는 96.8% 가 taker). 원인을 `-5022`(post-only 거부에 추격을 통째로 포기) 로 진단하고
① BBO 에서 1틱 물러나기 ② 거부에 break 하지 않고 재호가 로 고쳤다. 그런데 **검증에
일주일이 걸린다** — 이 전략은 0.2건/일 이라 체결이 그만큼 드물다.

검증하려는 건 전략이 아니라 `limit_then_market` 의 동작이다. 그러면 신호를 기다릴 이유가
없다. 최소 수량으로 진입→즉시 청산을 N 번 돌리면 같은 표본을 30분에 얻는다.

    python3 -m tools.maker_probe --n 10            # 테스트넷, 최소 수량으로 10회
    python3 -m tools.maker_probe --n 3 --dry       # 주문 없이 호가·수량·설정만 확인

결과는 fill_log 에 그대로 쌓이므로 `tools/report.py` 로도 읽힌다.

⚠️ 한계: 테스트넷 호가는 메인넷보다 얇아 **체결 확률 자체는 다를 수 있다.** 하지만
'-5022 가 나는가 · 재호가로 버티는가'는 그대로 재현되고, 그게 진단의 핵심이었다.

안전장치(전부 기본 켜짐):
  · 메인넷은 --allow-mainnet 없이는 거부한다(진짜 돈이 나간다).
  · 트레이더가 포지션을 들고 있으면 거부한다 — 프로브 주문이 봇의 회계를 오염시킨다.
  · 트레이더가 매매 중이면 거부한다(--force 로 무시 가능하나 권하지 않는다).
  · 무슨 일이 있어도 마지막엔 시장가로 평평하게 만든다(finally).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import fill_log                                          # noqa: E402
from engine.binance_broker import BinanceBroker, OrderError          # noqa: E402
from engine.env import load_dotenv                                   # noqa: E402
from engine.executor import _testnet_flag, api_keys                  # noqa: E402


def _guard_trader(state_path: str, force: bool) -> None:
    """봇과 충돌하지 않는지 확인. 포지션이 있으면 무조건 거부한다.

    프로브가 낸 주문은 봇의 사이드카·원장에 없다. 봇이 그걸 '외부 진입'으로 보고 인계하거나
    수량을 맞추려 들면 회계가 엉킨다 — 지금까지 사고가 난 자리가 정확히 거기다.
    """
    try:
        with open(state_path, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        print(f"  [주의] {state_path} 를 못 읽었습니다 — 봇 상태를 모르는 채로 진행합니다.")
        return
    if st.get("position"):
        raise SystemExit("거부: 트레이더가 포지션 보유 중입니다. 청산 후 다시 실행하세요.")
    if not st.get("paused"):
        if not force:
            raise SystemExit("거부: 트레이더가 매매 중입니다(멈춤 아님). "
                             "디스코드에서 ⏸ 멈춤 후 실행하거나 --force 를 주세요.")
        print("  [주의] 트레이더가 매매 중인데 --force 로 진행합니다 — 포지션이 겹칠 수 있습니다.")


def _min_qty(b: BinanceBroker, price: float) -> float:
    """거래소 최소 주문을 넘는 **가장 작은** 수량. 프로브는 싸야 한다."""
    lim = (b.market().get("limits") or {})
    min_qty = float(((lim.get("amount") or {}).get("min")) or 0) or 0.0
    min_cost = float(((lim.get("cost") or {}).get("min")) or 0) or 0.0
    qty = max(min_qty, (min_cost / price) if price > 0 else 0.0)
    step = b.round_qty(qty)
    # 절사로 최소 미달이 되면 한 스텝 올린다(내림 때문에 영원히 거부되는 걸 막는다).
    for _ in range(5):
        if step > 0:
            try:
                b.check_order_size(step, price)
                return step
            except OrderError:
                pass
        qty *= 1.5
        step = b.round_qty(qty)
    raise SystemExit("최소 주문 수량을 못 구했습니다 — 심볼/잔고를 확인하세요.")


def _summary(recs: list, kind: str) -> None:
    sub = [r for r in recs if r.get("kind") == kind]
    if not sub:
        return
    mr = [r["makerRatio"] for r in sub if r.get("makerRatio") is not None]
    orders = [r["orders"] for r in sub if r.get("orders")]
    label = "진입" if kind == "entry" else "청산"
    print(f"  ── {label} {len(sub)}건 ──")
    if mr:
        full = sum(1 for x in mr if x >= 99.9)
        zero = sum(1 for x in mr if x <= 0.1)
        print(f"    maker 비율 평균 {statistics.mean(mr):.1f}% · 전부 maker {full}건 · 전부 taker {zero}건")
    if orders:
        print(f"    지정가 재시도 평균 {statistics.mean(orders):.1f}회 · 최대 {max(orders)}회")


def probe(b: BinanceBroker, n: int, symbol: str, dry: bool) -> list:
    from engine import executor as ex_mod

    recs = []
    for i in range(1, n + 1):
        bid, ask = b.bbo()
        qty = _min_qty(b, ask)
        back = b.price_tick() * b.passive_ticks
        print(f"\n[{i}/{n}] 호가 {bid}/{ask} · 수량 {qty} · 지정가 {b.round_price(bid - back)} "
              f"(BBO-{b.passive_ticks}틱) · 대기 {ex_mod.DEFAULT_FILL_TIMEOUT}초×{ex_mod.DEFAULT_MAKER_ATTEMPTS}회",
              flush=True)
        if dry:
            continue
        # ---- 진입: 엔진과 **같은 설정**으로 (그걸 검증하는 게 목적이다) ----
        fill = b.limit_then_market("buy", qty, ex_mod.DEFAULT_FILL_TIMEOUT,
                                   max_attempts=ex_mod.DEFAULT_MAKER_ATTEMPTS)
        try:
            if fill.qty <= 0:
                print("    진입 미체결 — 건너뜀", flush=True)
                continue
            rec = fill_log.record(kind="entry", symbol=symbol, side=1, expected_price=bid,
                                  expected_qty=qty, fill=fill, intended_maker=True,
                                  network="testnet" if b.testnet else "mainnet",
                                  at_ms=int(time.time() * 1000))
            recs.append(rec)
            print(f"    {fill_log.summary(rec)}", flush=True)
            # ---- 청산: 엔진의 청산 설정으로 ----
            out = b.limit_then_market("sell", b.round_qty(fill.qty),
                                      ex_mod.DEFAULT_EXIT_FILL_TIMEOUT, reduce_only=True,
                                      max_attempts=ex_mod.DEFAULT_EXIT_MAKER_ATTEMPTS)
            rec = fill_log.record(kind="exit", symbol=symbol, side=1, expected_price=ask,
                                  expected_qty=fill.qty, fill=out, reason="probe",
                                  intended_maker=True,
                                  network="testnet" if b.testnet else "mainnet",
                                  at_ms=int(time.time() * 1000))
            recs.append(rec)
            print(f"    {fill_log.summary(rec)}", flush=True)
        finally:
            # ★ 무슨 일이 있어도 평평하게 두고 나간다. 프로브가 포지션을 남기면 봇이
            #   그걸 물려받아 회계가 엉킨다 — 그게 이 도구가 만들 수 있는 최악이다.
            _flatten(b)
    return recs


def _flatten(b: BinanceBroker) -> None:
    try:
        pos = b.position()
    except Exception as e:
        print(f"    [정리] 포지션 조회 실패: {e} — **직접 확인하세요**", flush=True)
        return
    if not pos:
        return
    qty = b.round_qty(abs(float(pos["qty"])))
    print(f"    [정리] 잔여 포지션 {pos['side']} {qty} → 시장가 청산", flush=True)
    try:
        b.market_order("sell" if pos["side"] == 1 else "buy", qty, reduce_only=True)
    except Exception as e:
        print(f"    [정리 실패] {e} — **거래소에서 직접 확인하세요**", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="maker 체결 정책 실측(진입→즉시청산 반복)")
    ap.add_argument("--n", type=int, default=5, help="반복 횟수(기본 5)")
    ap.add_argument("--symbol", default=None, help="심볼(기본: control.json 의 매매 심볼)")
    ap.add_argument("--dry", action="store_true", help="주문 없이 호가·수량·설정만 출력")
    ap.add_argument("--force", action="store_true", help="트레이더가 매매 중이어도 진행")
    ap.add_argument("--allow-mainnet", action="store_true", help="메인넷 허용(진짜 돈)")
    ap.add_argument("--state", default="data/state.json")
    a = ap.parse_args()

    load_dotenv()
    testnet = _testnet_flag()
    if not testnet and not a.allow_mainnet:
        raise SystemExit("거부: 메인넷입니다. 진짜 돈이 나갑니다 — 정말이면 --allow-mainnet 을 주세요.")

    symbol = a.symbol
    if not symbol:
        from engine import control
        symbol = (control.get_bot_config().get("symbol")
                  or (control.read_control().get("collect_symbols") or ["BTCUSDC"])[0])

    if not a.dry:
        _guard_trader(a.state, a.force)

    key, secret = api_keys(testnet)
    if not key or not secret:
        raise SystemExit("거부: API 키가 없습니다(.env 확인).")
    b = BinanceBroker(key, secret, testnet, symbol)
    print(f"maker 프로브 · {symbol} · {'테스트넷' if testnet else '★메인넷★'} · {a.n}회"
          + (" · DRY" if a.dry else ""))

    recs = []
    try:
        recs = probe(b, a.n, symbol, a.dry)
    finally:
        if not a.dry:
            _flatten(b)                      # 중단(Ctrl+C)해도 포지션을 남기지 않는다

    if recs:
        print("\n=== 실측 ===")
        _summary(recs, "entry")
        _summary(recs, "exit")
        print("\n  (fill_log 에도 쌓였습니다 — tools/report.py 로 누적 확인)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
