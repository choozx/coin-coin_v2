"""워밍업 — 지표가 값을 가질 만큼 창을 확보하는가. 네트워크 없음.

배경(실측): 라이브 창이 `warmup_days=10` 고정이었다. 그런데 10일은 타임프레임에 따라
1m 14,400봉 / 15m 961봉 / 4h 61봉 / **1d 11봉** 이다. EMA(200)·HAWKEYE(200) 를 쓰는
4h·1d 프리셋은 지표가 **영원히 NaN** 이었고, NaN 은 evaluate 에서 항상 false 라
**한 번도 진입하지 않는다**. 그러면서 로그엔 '진입 조건 미충족'만 쌓여 정상처럼 보인다.

조용한 실패라 아무도 몰랐을 것이다 — 그래서 ① 창을 프리셋에 맞춰 늘리고
② 그래도 NaN 이면 기동 시 알린다.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import conditions as C                 # noqa: E402


EMA200 = {"indicator": "EMA", "period": 200}
HAWK = {"indicator": "HAWKEYE", "period": 200, "params": {"divisor": 1.5}}
QQE = {"indicator": "QQE_MOD", "period": 6, "params": {"bb_length": 50, "smoothing": 5}}
MACD = {"indicator": "MACD", "params": {"fast": 12, "slow": 26, "signal": 9}}


def test_lookback_reads_period_and_period_like_params():
    """period 뿐 아니라 slow·bb_length 처럼 '기간처럼 쓰이는' params 도 봐야 한다."""
    assert C.operand_lookback(EMA200) == 200
    assert C.operand_lookback(QQE) == 50          # period 6 보다 bb_length 50 이 길다
    assert C.operand_lookback(MACD) == 26         # slow 가 지배한다
    assert C.operand_lookback({"source": "close"}) == 0
    assert C.operand_lookback(30) == 0            # 상수


def test_max_lookback_walks_the_whole_tree():
    node = {"op": "AND", "children": [
        {"left": {"source": "close"}, "cmp": ">", "right": EMA200},
        {"left": QQE, "cmp": ">", "right": 0}]}
    assert C.max_lookback(node) == 200


def test_required_bars_allows_for_recursive_convergence():
    """재귀 지표는 기간만큼만 있으면 값은 나오지만 **수렴하지 않는다**(unstable period)."""
    node = {"left": {"source": "close"}, "cmp": ">", "right": EMA200}
    assert C.required_warmup_bars(node) == 200 * C.UNSTABLE_MULT


def test_required_bars_has_a_floor():
    """짧은 지표만 써도 최소한은 확보한다 — SuperTrend(14) 하나여도 14봉으론 못 쓴다."""
    node = {"left": {"indicator": "SUPERTREND_DIR", "period": 14}, "cmp": ">", "right": 0}
    assert C.required_warmup_bars(node) == C.MIN_WARMUP_BARS


def test_required_bars_tolerates_missing_or_empty_nodes():
    """entryRules 만 쓰는 프리셋은 entry 가 None 이다 — 그걸로 깨지면 안 된다."""
    assert C.required_warmup_bars(None) == C.MIN_WARMUP_BARS
    assert C.required_warmup_bars(None, {}, {"op": "AND", "children": []}) == C.MIN_WARMUP_BARS


def test_warmup_days_scale_with_timeframe():
    """★ 이게 버그의 핵심 — 같은 봉수라도 상위 TF 는 훨씬 긴 기간이 필요하다.

    10일 고정이면 4h 는 61봉, 1d 는 11봉뿐이라 200기간 지표가 영영 NaN 이었다.
    """
    from engine.candles import TIMEFRAME_MINUTES
    node = {"left": {"source": "close"}, "cmp": ">", "right": EMA200}
    bars = C.required_warmup_bars(node)
    days = {tf: bars * TIMEFRAME_MINUTES[tf] / (24 * 60) for tf in ("15m", "1h", "4h", "1d")}
    assert days["15m"] < 10, "저TF 는 기존 10일 안에 들어온다(동작 불변)"
    assert days["4h"] > 10 and days["1d"] > days["4h"], "상위 TF 는 창을 늘려야 한다"
