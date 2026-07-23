"""백테스트 ↔ 라이브 엔진 대조 — 같은 캔들·프리셋이면 같은 매매가 나와야 한다.

왜 필요한가: 두 경로가 per-bar 로직을 **각각 구현**하고 있다.
  backtest.run()  의 루프        (backtest.py, `for t in range(n)`)
  LiveTrader._step() (live.py)   — 주석에 "backtest.run() 루프와 동일 순서"라고만 적힘.
사이징·청산가 같은 primitive는 공유하지만 **순서와 조건은 사람이 맞춰둔 것**이라,
한쪽만 고쳐도 나머지 테스트는 전부 통과한다. 그 차이는 실거래에서야 드러난다.

이 테스트는 (1) 지금 두 경로가 실제로 일치하는지 못박고,
(2) step() 추출 이후에도 남아 두 경로가 다시 갈라지는 걸 막는다.

⚠️ 임포트 순서 주의: control/settings 의 기본 경로는 **모듈 로드 시점**에 확정되고
   함수 기본인자로 묶인다(나중에 상수를 바꿔도 안 먹는다). 그래서 engine.live 를
   임포트하기 **전에** 환경변수로 임시 경로를 심는다 — 안 그러면 개발자의 실제
   data/control.json("trader": "paused")을 읽어 라이브만 진입을 0건 하고,
   대조가 무의미해진다.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="parity-")
os.environ["CONTROL_PATH"] = os.path.join(_TMP, "control.json")      # 없는 파일 → trader "running"
os.environ["SETTINGS_PATH"] = os.path.join(_TMP, "settings.json")    # 없는 파일 → 가드레일 기본(끔)
os.environ["STATE_PATH"] = os.path.join(_TMP, "state.json")

import numpy as np                                          # noqa: E402

from engine import control, settings, synthetic             # noqa: E402
from engine.backtest import BacktestConfig, run             # noqa: E402
from engine.candles import Candles                          # noqa: E402
from engine.executor import PaperExecutor                   # noqa: E402
from engine.live import LiveTrader                          # noqa: E402
from engine.preset import Preset                            # noqa: E402

EQUITY = 10_000.0


def _isolated() -> bool:
    """control/settings 가 임시 경로를 잡았는지(임포트 순서가 지켜졌는지)."""
    return (control.DEFAULT_PATH == os.environ["CONTROL_PATH"]
            and settings.SETTINGS_PATH == os.environ["SETTINGS_PATH"])


def _preset(**over) -> Preset:
    d = {"schemaVersion": "1.0", "name": "parity",
         "market": {"exchange": "binance-futures", "symbol": "BTCUSDT",
                    "timeframe": "5m", "direction": "long"},
         "entry": {"left": {"source": "close"}, "cmp": ">",
                   "right": {"indicator": "SMA", "period": 10}},
         "exit": {"takeProfit": {"type": "percent", "value": 0.6},
                  "stopLoss": {"type": "percent", "value": 0.4}},
         "sizing": {"leverage": 3, "marginMode": "isolated",
                    "size": {"type": "equityPercent", "value": 20}}}
    d.update(over)
    return Preset.from_dict(d, validate=False)


def _cfg() -> BacktestConfig:
    # 펀딩까지 켜서 정산 시점(8시간 경계)의 처리 순서도 대조에 포함시킨다.
    return BacktestConfig(initial_equity=EQUITY, taker_fee=0.0005,
                          maker_fee=0.0002, funding_rate=0.0001)


_ledger_seq = 0


def _fresh_trader(preset, cfg):
    """매번 '빈 원장'으로 시작하는 LiveTrader.

    LiveTrader는 생성 시 원장을 읽어 잔고·이력을 복원한다(재시작 대비 기능).
    원장을 공유하면 앞 테스트의 거래가 복원돼 잔고→사이징→체결이 통째로 달라진다.
    """
    global _ledger_seq
    _ledger_seq += 1
    ex = PaperExecutor(equity=EQUITY, taker_fee=cfg.taker_fee, maker_fee=cfg.maker_fee)
    trader = LiveTrader(preset, ex, cfg, mode="paper",
                        ledger_path=os.path.join(_TMP, f"trades-{_ledger_seq}.db"))
    assert not ex.trades and ex.equity() == EQUITY, "원장 격리 실패 — 이전 거래가 복원됐다"
    return trader, ex


def _live_trades(base, preset, cfg):
    """라이브 엔진에 캔들을 통째로 주입해 돌린 결과(청산된 거래 목록)."""
    trader, ex = _fresh_trader(preset, cfg)
    trader.poll_once(base=base)          # base 주입 → 폴링·전략전환 없이 순수 재생
    return ex.trades, ex.position


def _head(base: Candles, n: int) -> Candles:
    """앞에서 n개만 잘라낸 Candles(실시간에 캔들이 조금씩 도착하는 상황 재현)."""
    return Candles(base.open_time[:n], base.open[:n], base.high[:n], base.low[:n],
                   base.close[:n], base.volume[:n], base.timeframe_min,
                   None if base.taker_buy is None else base.taker_buy[:n])


def _fmt(t):
    """비교 키. 백테스트·라이브가 이제 같은 Trade 타입을 쓴다(둘 다 exit_reason)."""
    return (t.side, int(t.entry_time), round(float(t.entry_price), 6),
            int(t.exit_time), round(float(t.exit_price), 6),
            round(float(t.qty), 8), t.exit_reason)


def test_backtest_and_live_take_the_same_trades():
    """같은 입력 → 같은 매매(방향·시각·체결가·수량·청산사유)."""
    assert _isolated(), "임포트 순서 깨짐 — engine.live 가 먼저 로드되어 실제 data/ 를 읽는다"
    base = synthetic.generate(n_minutes=60 * 24 * 20, seed=7)
    cfg = _cfg()

    bt = run(base, _preset(), cfg)
    live_trades, live_pos = _live_trades(base, _preset(), cfg)

    assert bt.num_trades > 5, f"대조가 의미 있으려면 거래가 있어야 함 (실제 {bt.num_trades}건)"

    # 백테스트는 마지막에 잔여 포지션을 강제청산한다(reason="signal"). 라이브는 계속 들고 간다.
    bt_trades = list(bt.trades)
    if live_pos is not None:
        assert bt_trades, "백테스트에 강제청산 거래가 있어야 함"
        forced = bt_trades.pop()
        assert forced.exit_time == int(base.open_time[-1]) + 60_000, (
            "마지막 거래가 강제청산이 아님 — 꼬리 처리 가정이 바뀌었다")

    assert len(bt_trades) == len(live_trades), (
        f"거래 수 불일치: 백테스트 {len(bt_trades)} vs 라이브 {len(live_trades)} — "
        f"두 경로의 per-bar 순서/조건이 갈라졌다")

    for i, (b, l) in enumerate(zip(bt_trades, live_trades)):
        assert _fmt(b) == _fmt(l), (
            f"{i}번째 거래 불일치:\n  백테스트 {_fmt(b)}\n  라이브   {_fmt(l)}")


def test_backtest_and_live_agree_on_pnl():
    """손익·수수료·펀딩까지 동일해야 한다(체결이 같아도 정산이 갈릴 수 있다)."""
    assert _isolated()
    base = synthetic.generate(n_minutes=60 * 24 * 20, seed=11)
    cfg = _cfg()

    bt = run(base, _preset(), cfg)
    live_trades, live_pos = _live_trades(base, _preset(), cfg)

    bt_trades = list(bt.trades)
    if live_pos is not None:
        bt_trades.pop()

    for i, (b, l) in enumerate(zip(bt_trades, live_trades)):
        for field in ("pnl", "fees", "funding"):
            assert abs(getattr(b, field) - getattr(l, field)) < 1e-9, (
                f"{i}번째 거래 {field} 불일치: 백테스트 {getattr(b, field)} vs 라이브 {getattr(l, field)}")


def test_maker_timeout_parity():
    """passive-then-aggressive(makerLimit + makerTimeoutSeconds)도 백테스트↔라이브 일치.

    이 경로는 Stepper에 '대기 지정가' 상태를 둔다 — 백테스트와 라이브가 그 상태를
    같은 순서로 풀어야 체결(maker/taker)·시각이 일치한다."""
    assert _isolated()
    base = synthetic.generate(n_minutes=60 * 24 * 15, seed=5)
    cfg = _cfg()
    over = {"execution": {"entryType": "makerLimit", "makerTimeoutSeconds": 120}}

    bt = run(base, _preset(**over), cfg)
    live_trades, live_pos = _live_trades(base, _preset(**over), cfg)

    assert bt.num_trades > 5, f"대조가 의미 있으려면 거래가 있어야 함 (실제 {bt.num_trades}건)"
    bt_trades = list(bt.trades)
    if live_pos is not None:
        bt_trades.pop()
    assert len(bt_trades) == len(live_trades), (
        f"maker-timeout 거래 수 불일치: 백테스트 {len(bt_trades)} vs 라이브 {len(live_trades)}")
    for i, (b, l) in enumerate(zip(bt_trades, live_trades)):
        assert _fmt(b) == _fmt(l), (
            f"{i}번째 maker-timeout 거래 불일치:\n  백테스트 {_fmt(b)}\n  라이브   {_fmt(l)}")


def test_live_replay_is_chunk_independent():
    """캔들을 한 번에 주든 나눠서 주든 결과가 같아야 한다(폴링 경계 무관).

    실거래는 1분마다 조각으로 들어온다 — 조각 경계가 결과를 바꾸면 백테스트와
    영영 안 맞는다. 대조 테스트가 '한 번에 주입'만 검사하면 이 차이를 놓친다.
    """
    assert _isolated()
    base = synthetic.generate(n_minutes=60 * 24 * 10, seed=3)
    cfg = _cfg()

    whole, _ = _live_trades(base, _preset(), cfg)

    trader, ex = _fresh_trader(_preset(), cfg)
    step = 137                                  # 분 경계와 안 맞아떨어지는 크기로 일부러
    for end in range(step, len(base.open_time) + step, step):
        trader.poll_once(base=_head(base, min(end, len(base.open_time))))

    assert len(whole) == len(ex.trades), (
        f"조각 주입 시 거래 수가 달라짐: 통째 {len(whole)} vs 조각 {len(ex.trades)}")
    for i, (w, c) in enumerate(zip(whole, ex.trades)):
        assert _fmt(w) == _fmt(c), (
            f"{i}번째 거래가 조각 경계에 따라 달라짐:\n  통째 {_fmt(w)}\n  조각 {_fmt(c)}")


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
            passed += 1
        except Exception:
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
