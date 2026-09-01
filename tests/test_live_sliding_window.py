"""라이브의 슬라이딩 윈도우 — 기간 판정이 배열 인덱스에 의존하면 안 된다.

배경(실제 사고): 라이브는 매 폴마다 `ensure_days(symbol, 10일, end_ms=now)` 로 **최근 N일**
윈도우를 새로 받는다. 창이 시간과 같은 속도로 미끄러지므로 **마지막 신호봉의 인덱스가
변하지 않는다**(실측: 15분이 지나도 sb=960 그대로).

그런데 쿨다운(`last_exit_sb`)과 시간청산(`entry_signal_idx`)이 배열 인덱스 기준이었다.
결과: `sb - last_exit_sb` 가 0 근처에 고정 → **청산 후 재진입이 영구 차단**되고, timeStop 은
영영 발동하지 않았다. 운영 로그가 그걸 그대로 보여줬다 — '0/3봉 경과' 와 '1/3봉 경과' 만
있고 '2/3','3/3' 이 단 한 건도 없었다.

백테스트는 배열이 고정이라 정상이었고, backtest↔live 대조 테스트도 고정 배열을 주입해
돌리므로 못 잡았다. 그래서 이 파일은 **창이 미끄러지는 상황 자체**를 재현한다.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.backtest import BacktestConfig, Stepper           # noqa: E402
from engine.candles import Candles, resample, signal_close_index  # noqa: E402
from engine.conditions import SeriesResolver                  # noqa: E402
from engine.executor import PaperExecutor                     # noqa: E402
from engine.preset import Preset                              # noqa: E402

MIN = 60_000
TF_MIN = 5                       # 5분봉 프리셋
WINDOW_BARS = 600                # 라이브가 유지하는 1분봉 창 길이(고정 폭)


def _base(start_min: int, n: int = WINDOW_BARS) -> Candles:
    """start_min 분부터 n개 1분봉. start_min 을 늘리면 창이 미끄러진다(라이브와 같은 모양)."""
    t = np.arange(start_min, start_min + n, dtype=np.int64) * MIN
    px = np.full(n, 100.0)
    return Candles(open_time=t, open=px, high=px + 0.1, low=px - 0.1, close=px,
                   volume=np.full(n, 10.0), timeframe_min=1)


def _stepper(filt):
    preset = Preset({
        "schemaVersion": "1.0", "name": "sliding",
        "market": {"exchange": "binance-futures", "symbol": "BTCUSDT",
                   "timeframe": f"{TF_MIN}m", "direction": "long"},
        "entry": {"left": {"source": "close"}, "cmp": ">", "right": 0},   # 항상 참
        "exit": {"stopLoss": {"type": "percent", "value": 50.0}},
        "sizing": {"leverage": 1, "marginMode": "isolated",
                   "size": {"type": "equityPercent", "value": 10}},
        "filter": filt,
    })
    return Stepper(preset, BacktestConfig(), PaperExecutor(equity=10_000.0))


def _last_sb(base):
    bar_of, _ = signal_close_index(base, TF_MIN)
    return int(bar_of[len(base) - 1])


def test_the_window_really_slides_and_index_stays_put():
    """전제 확인 — 창이 미끄러지면 **같은 인덱스가 다른 시각**을 가리킨다.

    이게 참이라서 인덱스 기반 기간 판정이 깨졌다. 전제가 무너지면 아래 테스트도 의미가 없다.
    """
    a, b = _base(0), _base(TF_MIN * 4)          # 4 신호봉만큼 뒤로 미끄러진 창
    sa, sb_ = _last_sb(a), _last_sb(b)
    assert sa == sb_, "창 폭이 같으면 마지막 신호봉 인덱스는 그대로다"
    ta = int(resample(a, TF_MIN).open_time[sa])
    tb = int(resample(b, TF_MIN).open_time[sb_])
    assert tb - ta == TF_MIN * 4 * MIN, "그런데 그 인덱스가 가리키는 시각은 움직였다"


def test_cooldown_expires_as_time_passes_on_a_sliding_window():
    """★ 회귀: 창이 미끄러져도 쿨다운은 **시간이 지나면 풀려야** 한다."""
    st = _stepper({"cooldownBars": 3})
    first = _base(0)
    sig0 = resample(first, TF_MIN)
    st.last_exit_time = int(sig0.open_time[_last_sb(first)])      # 방금 청산

    seen = []
    for shift in range(0, 5):                                     # 신호봉 5개만큼 시간 경과
        w = _base(TF_MIN * shift)
        sig = resample(w, TF_MIN)
        side, block = st.entry_block(sig, SeriesResolver(sig), _last_sb(w), int(sig.open_time[-1]))
        seen.append(block)

    assert seen[0] is not None and "쿨다운" in seen[0], "직후엔 막혀야 한다"
    assert seen[-1] is None, f"3봉이 지나면 풀려야 한다 — 실제: {seen}"
    # 인덱스 기준이었다면 전부 '0/3' 이나 '1/3' 에 갇혀 seen[-1] 도 쿨다운이었다.
    assert any("2/3" in (b or "") or b is None for b in seen), f"경과가 실제로 늘어야 한다: {seen}"


def test_time_stop_fires_on_a_sliding_window():
    """★ 회귀: timeStop 도 인덱스가 아니라 시간으로 세야 라이브에서 발동한다."""
    st = _stepper({})
    st.preset.exit["timeStop"] = {"maxBars": 3}

    w0 = resample(_base(0), TF_MIN)
    st.ex.position = None
    base0 = _base(0)
    bar_of, is_close = signal_close_index(base0, TF_MIN)
    atr = np.full(len(w0), 1.0)
    # 첫 창에서 진입시킨다
    for t in range(len(base0)):
        st.step(base0, w0, bar_of, is_close, atr, SeriesResolver(w0), t)
    assert st.ex.position is not None, "항상 참 조건이라 진입해 있어야 한다"
    entry_time = st.ex.position.entry_signal_time

    # 창을 4 신호봉만큼 미끄러뜨려 이어서 처리 → 시간청산이 걸려야 한다
    base1 = _base(TF_MIN * 4)
    w1 = resample(base1, TF_MIN)
    bar_of1, is_close1 = signal_close_index(base1, TF_MIN)
    atr1 = np.full(len(w1), 1.0)
    st._last_ot = None
    for t in range(len(base1)):
        st.step(base1, w1, bar_of1, is_close1, atr1, SeriesResolver(w1), t)
    closed = [tr for tr in st.ex.trades if tr.exit_reason == "time"]
    assert closed, (f"시간청산이 발동해야 한다(진입봉 {entry_time}, "
                    f"창이 {TF_MIN*4}분 이동) — 인덱스 기준이면 영영 안 걸린다")


# ---- 과거 replay 방지: 기준봉(_last_ot)이 사라지는 자리들 ----

def _trader(tmp_env, base):
    """네트워크 없이 도는 LiveTrader — _fetch 를 고정 창으로 대체한다."""
    from engine.backtest import BacktestConfig
    from engine.executor import PaperExecutor
    from engine import live as L
    preset = Preset({
        "schemaVersion": "1.0", "name": "replay",
        "market": {"exchange": "binance-futures", "symbol": "BTCUSDT",
                   "timeframe": f"{TF_MIN}m", "direction": "long"},
        "entry": {"left": {"source": "close"}, "cmp": ">", "right": 0},   # 항상 참 = 봉마다 신호
        "exit": {"timeStop": {"maxBars": 1}},
        "sizing": {"leverage": 1, "marginMode": "isolated",
                   "size": {"type": "equityPercent", "value": 10}},
    })
    tr = L.LiveTrader(preset, PaperExecutor(equity=10_000.0),
                      BacktestConfig(initial_equity=10_000.0),
                      state_path=os.path.join(tmp_env, "s.json"),
                      ledger_path=os.path.join(tmp_env, "t.db"),
                      strategy_path="x", mode="paper")
    tr._fetch = lambda now_ms: base
    return tr


def test_production_poll_never_replays_the_whole_window(tmp_path):
    """★ 회귀: 실운영에서 기준봉이 없어도 창 전체를 몰아 실행하면 안 된다.

    창이 10일이면 과거 신호 수십 건이 **지금 시세로** 한꺼번에 체결된다. 라이브면 실주문이다
    (실측: 심볼 변경 한 번에 한 폴에서 7트레이드=14주문 발생했다).
    """
    base = _base(0, n=WINDOW_BARS)
    tr = _trader(str(tmp_path), base)
    tr._last_ot = None                                   # 기준점이 없는 상태
    tr.poll_once(now_ms=int(base.open_time[-1]) + MIN)   # base 를 주입하지 않음 = 실운영 경로
    assert len(tr.ex.trades) <= 1, (
        f"과거 봉이 몰아서 실행됐다 — {len(tr.ex.trades)}건")
    assert tr._last_ot is not None, "처리 후엔 기준점이 잡혀 있어야 다음 폴이 안전하다"


def test_symbol_change_moves_the_cursor_instead_of_clearing_it(tmp_path):
    """★ 회귀: 봇 설정으로 심볼을 바꿔도 기준봉이 None 으로 남으면 안 된다.

    전략 전환 경로엔 이 처리가 있었는데 봇 설정 경로에만 빠져 있었다 — 같은 실수가 두 곳에
    갈라져 있으면 하나는 반드시 잊힌다. 그래서 _skip_to_latest 한 곳으로 모았다.
    """
    from engine import control
    base = _base(0, n=WINDOW_BARS)
    tr = _trader(str(tmp_path), base)
    tr.bootstrap(now_ms=int(base.open_time[-1]) + MIN)
    assert tr._last_ot is not None

    control.set_bot_config({"symbol": "ETHUSDT"}, path=os.path.join(str(tmp_path), "c.json"))
    tr._bot_cfg = {}                                     # 변경을 감지하도록
    import engine.control as C
    orig = C.get_bot_config
    C.get_bot_config = lambda *a, **k: {"symbol": "ETHUSDT"}
    try:
        tr._maybe_apply_bot_config()
    finally:
        C.get_bot_config = orig
    assert tr.preset.symbol == "ETHUSDT", "심볼이 실제로 바뀌어야 테스트가 의미 있다"
    assert tr._last_ot is not None, "심볼이 바뀌어도 기준봉은 최신으로 옮겨져 있어야 한다"


def test_cursor_survives_a_failed_fetch(tmp_path):
    """받아오기에 실패하면 기준점을 **유지**한다 — None 으로 두면 다음 폴이 통째로 replay 된다."""
    base = _base(0, n=WINDOW_BARS)
    tr = _trader(str(tmp_path), base)
    tr.bootstrap(now_ms=int(base.open_time[-1]) + MIN)
    before = tr._last_ot
    tr._fetch = lambda now_ms: (_ for _ in ()).throw(RuntimeError("네트워크 끊김"))
    tr._skip_to_latest("테스트")
    assert tr._last_ot == before
