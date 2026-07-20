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


def test_orderflow_delta_cvd():
    # taker_buy가 volume의 80% → delta_ratio=+0.6(매수우위), CVD 우상향.
    n = 30
    vol = np.full(n, 10.0)
    tb = np.full(n, 8.0)
    dr = ind.taker_delta_ratio(vol, tb)
    assert abs(dr[0] - 0.6) < 1e-9
    d = ind.taker_delta(vol, tb)
    assert abs(d[0] - 6.0) < 1e-9                 # 2*8-10
    c = ind.cvd(vol, tb)
    assert c[-1] > c[0] and (np.diff(c) > 0).all()  # 매수우위면 CVD 단조 증가
    # 매도우위(taker_buy=2) → delta_ratio 음수, CVD 하락
    dr2 = ind.taker_delta_ratio(vol, np.full(n, 2.0))
    assert (dr2 < 0).all()


def test_orderflow_condition_needs_taker_data():
    # taker_buy 없는 Candles에선 오더플로우 조건이 항상 false (NaN 처리).
    from engine.candles import Candles
    from engine.conditions import SeriesResolver, evaluate
    n = 20
    ot = (np.arange(n) * 60000).astype(np.int64)
    px = np.full(n, 100.0)
    c_no = Candles(ot, px, px, px, px, np.full(n, 10.0), 1)          # taker_buy=None
    node = {"left": {"indicator": "TAKER_DELTA_RATIO"}, "cmp": ">", "right": 0.1}
    assert evaluate(node, SeriesResolver(c_no), 10) is False
    c_yes = Candles(ot, px, px, px, px, np.full(n, 10.0), 1, taker_buy=np.full(n, 8.0))
    assert evaluate(node, SeriesResolver(c_yes), 10) is True


def test_hawkeye_bull_bear_neutral():
    rng = np.random.default_rng(1)
    n = 400
    close = np.cumsum(rng.normal(0, 1, n)) + 1000
    high = close + np.abs(rng.normal(0, 2, n))
    low = close - np.abs(rng.normal(0, 2, n))
    vol = np.abs(rng.normal(100, 30, n))
    hv = ind.hawkeye(high, low, close, vol, length=200, divisor=3.6)
    valid = hv[~np.isnan(hv)]
    assert valid.size > 0
    assert set(np.unique(valid)).issubset({-1.0, 0.0, 1.0})   # 세 상태만
    assert np.isnan(hv[0])                                     # 워밍업(이전봉 없음)
    assert np.isnan(hv[100])                                   # length=200 미달 구간
    # 강한 상승봉(종가>전봉중점)은 강세(+1) 후보 — 명시적 케이스
    h = np.array([10, 12.0]); l = np.array([8, 9.0]); c = np.array([9, 11.5]); v = np.array([100, 100.0])
    hv2 = ind.hawkeye(h, l, c, v, length=1, divisor=3.6)
    assert hv2[1] in (1.0, 0.0, -1.0)


def test_maker_limit_entry():
    from engine.backtest import BacktestConfig, run
    from engine.preset import Preset
    rng = np.random.default_rng(3)
    n = 2000
    close = np.cumsum(rng.normal(0, 1, n)) + 5000
    high = close + np.abs(rng.normal(0, 3, n))
    low = close - np.abs(rng.normal(0, 3, n))
    ot = (np.arange(n) * 60000).astype(np.int64)
    base = Candles(ot, close.copy(), high, low, close, np.full(n, 100.0), 1)

    def preset(execution):
        d = {"schemaVersion": "1.0", "name": "m",
             "market": {"exchange": "binance-futures", "symbol": "BTCUSDT", "timeframe": "1m", "direction": "long"},
             "entry": {"left": {"source": "close"}, "cmp": ">", "right": {"indicator": "SMA", "period": 5}},
             "exit": {"takeProfit": {"type": "percent", "value": 0.5}, "stopLoss": {"type": "percent", "value": 0.5}},
             "sizing": {"leverage": 2, "marginMode": "isolated", "size": {"type": "equityPercent", "value": 10}}}
        if execution:
            d["execution"] = execution
        return Preset.from_dict(d, validate=False)

    cfg = BacktestConfig(initial_equity=10000, taker_fee=0.0005, maker_fee=0.0, funding_rate=0.0)
    taker = run(base, preset(None), cfg)
    maker = run(base, preset({"entryType": "makerLimit"}), cfg)
    assert taker.num_trades > 0
    # 지정가도 종가에 그냥 체결(미체결 모델 없음) → 트레이드 수 동일
    assert maker.num_trades == taker.num_trades
    # maker 진입 수수료 0 → 총 수수료는 taker보다 작고 수익은 더 좋음
    assert maker.total_fees < taker.total_fees
    assert maker.total_return_pct >= taker.total_return_pct


def test_supertrend_flip_exit_side_aware():
    from engine.candles import Candles
    from engine.conditions import SeriesResolver
    from engine.backtest import _supertrend_flip_exit
    up = np.linspace(100, 140, 80)
    down = np.linspace(140, 100, 80)
    close = np.concatenate([up, down])
    ot = (np.arange(len(close)) * 300000).astype(np.int64)
    c = Candles(ot, close, close + 0.5, close - 0.5, close, np.full(len(close), 10.0), 5)
    r = SeriesResolver(c)
    st = {"period": 10, "multiplier": 3.0}
    _, d = ind.supertrend(c.high, c.low, c.close, 10, 3.0)
    flips_down = [i for i in range(1, len(d)) if d[i - 1] == 1 and d[i] == -1]
    assert flips_down, "하락 전환이 있어야 함"
    fi = flips_down[0]
    assert _supertrend_flip_exit(r, st, +1, fi) is True    # 롱은 하락전환에 청산
    assert _supertrend_flip_exit(r, st, -1, fi) is False    # 숏은 아님
    flips_up = [i for i in range(1, len(d)) if d[i - 1] == -1 and d[i] == 1]
    if flips_up:
        ui = flips_up[0]
        assert _supertrend_flip_exit(r, st, -1, ui) is True   # 숏은 상승전환에 청산
        assert _supertrend_flip_exit(r, st, +1, ui) is False


def test_qqe_mod_signal():
    rng = np.random.default_rng(2)
    n = 500
    close = np.cumsum(rng.normal(0, 1, n)) + 1000
    line, rsi_ma = ind.qqe(close, 6, 5, 3.0)
    v = rsi_ma[~np.isnan(rsi_ma)]
    assert (v >= 0).all() and (v <= 100).all()          # smoothedRSI는 0~100
    # 트레일링 라인도 0~100 근방
    lv = line[~np.isnan(line)]
    assert lv.size > 0 and lv.min() > -50 and lv.max() < 150
    sig = ind.qqe_mod(close, 6, 5, 3.0, 1.61, 3.0, 50, 0.35)
    sv = sig[~np.isnan(sig)]
    assert sv.size > 0
    assert set(np.unique(sv)).issubset({-1.0, 0.0, 1.0})  # 세 상태만


def test_supertrend_trend_and_flip():
    # 상승→하락 명확한 추세: SuperTrend가 방향을 따라가고 플립이 최소 1번 나야 함.
    up = np.linspace(100, 140, 60)
    down = np.linspace(140, 100, 60)
    close = np.concatenate([up, down])
    high = close + 0.5
    low = close - 0.5
    line, d = ind.supertrend(high, low, close, period=10, multiplier=3.0)
    valid = ~np.isnan(d)
    assert valid.any()
    # 방향은 ±1만
    assert set(np.unique(d[valid])).issubset({-1.0, 1.0})
    # 상승추세엔 라인이 가격 아래(지지), 하락추세엔 위(저항)
    up_idx = np.where(valid & (d == 1.0))[0]
    dn_idx = np.where(valid & (d == -1.0))[0]
    assert (line[up_idx] <= close[up_idx]).all()
    assert (line[dn_idx] >= close[dn_idx]).all()
    # 추세 반전이 있으니 방향 전환(플립)이 최소 1번
    assert (np.diff(d[valid]) != 0).any()


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
