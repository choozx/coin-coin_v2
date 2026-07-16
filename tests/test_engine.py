"""엔진 핵심 로직 검증. 실행: python3 -m pytest tests/ -v  (또는 이 파일 직접 실행)"""
from __future__ import annotations

import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.candles import Candles, resample, signal_close_index
from engine import indicators as ind
from engine import binance_math as bm
from engine.backtest import BacktestConfig, run
from engine.preset import Preset
from engine import synthetic


def _candles(rows, tf=1):
    a = np.array(rows, dtype=float)
    return Candles(a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3], a[:, 4], a[:, 5], tf)


MIN = 60_000


# ---- 리샘플 ----------------------------------------------------------------
def test_resample_5m():
    # 10개 1분봉 → 5분봉 2개
    rows = [[i * MIN, 100 + i, 100 + i + 2, 100 + i - 2, 100 + i + 1, 10] for i in range(10)]
    c = _candles(rows)
    r = resample(c, 5)
    assert len(r) == 2
    assert r.open[0] == 100          # 첫 봉 open
    assert r.close[0] == c.close[4]  # 5번째 close
    assert r.high[0] == max(x[2] for x in rows[:5])
    assert r.low[0] == min(x[3] for x in rows[:5])
    assert r.volume[0] == 50


def test_signal_close_index():
    rows = [[i * MIN, 1, 1, 1, 1, 1] for i in range(10)]
    c = _candles(rows)
    bar_of, is_close = signal_close_index(c, 5)
    assert is_close.sum() == 2               # 5분봉 2개 → 종료점 2개
    assert is_close[4] and is_close[9]        # 5번째, 10번째 1분봉이 종료점


# ---- 지표 ------------------------------------------------------------------
def test_sma_ema_basic():
    x = np.arange(1, 11, dtype=float)
    assert abs(ind.sma(x, 3)[2] - 2.0) < 1e-9
    assert np.isnan(ind.sma(x, 3)[1])
    e = ind.ema(x, 3)
    assert not np.isnan(e[-1])


def test_rsi_bounds():
    x = np.cumsum(np.random.default_rng(0).normal(0, 1, 200)) + 100
    r = ind.rsi(x, 14)
    valid = r[~np.isnan(r)]
    assert (valid >= 0).all() and (valid <= 100).all()


# ---- 청산가 공식 -----------------------------------------------------------
def test_liquidation_price_long():
    # EP=100, 10x, qty=1, MMR=0.004 → 약 90.36
    b = bm.MarginBracket(mmr=0.004, cum=0.0)
    liq = bm.liquidation_price(100, 1, 10, +1, b)
    assert 89 < liq < 91
    assert liq < 100  # 롱 청산가는 진입가 아래


def test_liquidation_price_short():
    b = bm.MarginBracket(mmr=0.004, cum=0.0)
    liq = bm.liquidation_price(100, 1, 10, -1, b)
    assert 109 < liq < 111
    assert liq > 100  # 숏 청산가는 진입가 위


def test_funding_direction():
    # 롱, 펀딩비율>0 → 지불(음수)
    assert bm.funding_fee(100, 1, +1, 0.0001) < 0
    # 숏, 펀딩비율>0 → 수취(양수)
    assert bm.funding_fee(100, 1, -1, 0.0001) > 0


# ---- ★ 핵심: 청산이 손절보다 먼저 ------------------------------------------
def test_liquidation_before_stop():
    """고레버리지 + 손절을 청산가보다 멀리 두면, 급락 시 손절이 아니라 청산돼야 한다.

    50x → 청산가 약 -2%. 손절 -5%(청산가 너머). 가격이 -3%까지 빠지면
    손절(-5%)은 안 닿지만 청산(-2%)은 닿음 → exit_reason == liquidation.
    """
    entry = 100.0
    # 1분봉: 진입 후 저가가 97(-3%)까지 빠지는 봉을 배치
    rows = [
        [0 * MIN, 100, 100.1, 99.9, 100, 10],   # 신호봉(1m TF) 진입 트리거용
        [1 * MIN, 100, 100.1, 97.0, 98.0, 10],  # 저가 97 → 청산가(-2%)만 닿음
        [2 * MIN, 98, 98.1, 97.9, 98, 10],
    ]
    base = _candles(rows, tf=1)

    preset = Preset.from_dict({
        "schemaVersion": "1.0",
        "name": "liq-test", "market": {"symbol": "BTCUSDT", "timeframe": "1m", "direction": "long"},
        "entry": {"left": {"source": "close"}, "cmp": ">", "right": 0},  # 항상 참 → 첫봉 진입
        "exit": {"stopLoss": {"type": "percent", "value": 5.0}},         # 손절 -5% (청산가 너머)
        "sizing": {"leverage": 50, "marginMode": "isolated",
                   "size": {"type": "equityPercent", "value": 100}},
    }, validate=False)

    cfg = BacktestConfig(initial_equity=1000, funding_rate=0.0,
                         bracket=bm.MarginBracket(mmr=0.004, cum=0.0))
    m = run(base, preset, cfg)
    assert m.num_trades >= 1
    assert m.trades[0].exit_reason == "liquidation", \
        f"손절이 아니라 청산이어야 함, got {m.trades[0].exit_reason}"


# ---- 전저점(swing) 손절 ----------------------------------------------------
def test_swing_low_stop():
    """전저점 손절: 손절가가 최근 lookback 봉 최저가 아래(버퍼 반영)에 놓여야.

    저가가 100→95로 계단식 하락(전저점 95 형성), 마지막 하락봉(close 95)에서 진입.
    다음 봉 저가 94가 전저점*(1-0.1%)≈94.9를 깨면서 손절.
    """
    lows = [100, 99, 98, 97, 96, 95]
    closes = [100, 100, 100, 100, 100, 95]     # 마지막 봉만 close<96 → 여기서 진입
    rows = [[i * MIN, 100, 100.6, lows[i], closes[i], 10] for i in range(6)]
    rows.append([6 * MIN, 95, 95.6, 94.0, 95, 10])
    base = _candles(rows, tf=1)

    preset = Preset.from_dict({
        "schemaVersion": "1.0", "name": "swing-test",
        "market": {"symbol": "BTCUSDT", "timeframe": "1m", "direction": "long"},
        "entry": {"left": {"source": "close"}, "cmp": "<", "right": 96},  # close 95인 봉에서만
        "exit": {"stopLoss": {"type": "swingLow", "lookback": 6, "bufferPercent": 0.1}},
        "sizing": {"leverage": 3, "marginMode": "isolated",
                   "size": {"type": "equityPercent", "value": 50}},
    }, validate=False)
    m = run(base, preset, BacktestConfig(initial_equity=1000, funding_rate=0))
    assert m.num_trades >= 1
    assert m.trades[0].exit_reason == "stop_loss"
    # 전저점 95의 -0.1% 버퍼 ≈ 94.9 근처에서 손절
    assert 94.5 < m.trades[0].exit_price < 95.1


# ---- 리스크 기반 사이징 ----------------------------------------------------
def test_risk_percent_sizing():
    """리스크%: 손절 맞으면 손실이 (자본 × 리스크%)에 근접해야 한다."""
    rows = [[0 * MIN, 100, 100.1, 99.9, 100, 10],
            [1 * MIN, 100, 100.1, 98.9, 99, 10]]  # 저가 98.9 → 손절 99(-1%) 터치
    base = _candles(rows, tf=1)
    preset = Preset.from_dict({
        "schemaVersion": "1.0", "name": "risk-test",
        "market": {"symbol": "BTCUSDT", "timeframe": "1m", "direction": "long"},
        # close>99.5 → 첫 봉(100)만 진입, 손절 후 둘째 봉(99)엔 재진입 안 함
        "entry": {"left": {"source": "close"}, "cmp": ">", "right": 99.5},
        "exit": {"stopLoss": {"type": "percent", "value": 1.0}},  # -1% 손절
        "sizing": {"leverage": 10, "marginMode": "isolated",
                   "size": {"type": "riskPercent", "value": 2.0}},  # 자본의 2% 리스크
    }, validate=False)
    equity0 = 1000.0
    m = run(base, preset, BacktestConfig(initial_equity=equity0, funding_rate=0,
                                         taker_fee=0, maker_fee=0))  # 수수료 0으로 순수 확인
    assert m.num_trades == 1
    loss = equity0 - m.final_equity
    # 목표 리스크 = 1000 × 2% = 20. 손절 정확히 -1%에서 → 손실 ≈ 20
    assert abs(loss - 20.0) < 0.5, f"리스크 목표 20 근처여야, got {loss}"


def test_risk_percent_needs_stop():
    """리스크%인데 손절 없으면 진입 불가(트레이드 0)."""
    base = synthetic.generate(60 * 24 * 3, seed=1)
    preset = Preset.from_dict({
        "schemaVersion": "1.0", "name": "no-stop",
        "market": {"symbol": "BTCUSDT", "timeframe": "5m", "direction": "long"},
        "entry": {"left": {"source": "close"}, "cmp": ">", "right": 0},
        "exit": {"takeProfit": {"type": "percent", "value": 1.0}},  # 손절 없음
        "sizing": {"leverage": 5, "marginMode": "isolated",
                   "size": {"type": "riskPercent", "value": 1.0}},
    }, validate=False)
    m = run(base, preset)
    assert m.num_trades == 0


# ---- 스모크: 예시 프리셋 실행 ----------------------------------------------
def test_example_presets_smoke():
    base = synthetic.generate(60 * 24 * 20, seed=7)  # 20일
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("rsi-oversold-long.json", "macd-cross-both.json"):
        preset = Preset.load(os.path.join(root, "presets", "examples", name))
        m = run(base, preset)
        assert m.final_equity > 0            # 파산 없이 완주
        assert isinstance(m.summary(), str)


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
