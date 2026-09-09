"""귀무 게이트 — "우연보다 나은가"를 최적화기에도 물린다.

배경: 최적화기는 IS/OOS 만 봤다. 그건 '다른 구간에서도 되나' 를 물을 뿐이다. 지금 라이브
프리셋이 그 차이의 산물이다 — 이름부터 '최적화' 이고 IS/OOS 는 통과했지만 K 에서 귀무
p95(+59.35%)에 한참 못 미쳐(백분위 86) 기각됐다.
"""
from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import null_model as nm                        # noqa: E402
from engine.candles import Candles                         # noqa: E402


def _base(n=6000, seed=3):
    rng = np.random.default_rng(seed)
    px = 100 * np.exp(np.cumsum(rng.normal(0, 0.0006, n)))
    ot = (np.arange(n) * 60_000).astype(np.int64)
    return Candles(ot, px, px * 1.001, px * 0.999, px, np.full(n, 10.0), 1)


def test_simulate_is_deterministic_for_a_seed():
    """같은 시드면 같은 분포 — 판정이 실행마다 달라지면 판정이 아니다."""
    b = _base()
    a1 = nm.simulate(b, 15, 50, 10, "long", samples=200, seed=5)
    a2 = nm.simulate(b, 15, 50, 10, "long", samples=200, seed=5)
    assert np.allclose(a1, a2)


def test_more_trades_costs_more_fees():
    """★ 회전율이 높으면 귀무도 그만큼 깎여야 공정하다.

    L 에서 '덜 사고팔았다'는 이유로 전략이 이긴 걸 신호로 오독한 적이 있다.
    """
    b = _base()
    few = np.median(nm.simulate(b, 15, 10, 10, "long", samples=400, seed=1, taker_fee=0.0005))
    many = np.median(nm.simulate(b, 15, 200, 10, "long", samples=400, seed=1, taker_fee=0.0005))
    assert many < few


def test_gate_flags_negative_returns():
    """★ 깊게 음수인 구간에선 귀무를 넘어도 엣지가 아니다 — 그 사실을 결과에 싣는다."""
    d = np.array([-95.0, -90.0, -85.0, -80.0])
    g = nm.gate(-70.0, d)
    assert g["beatsNull"] is True and g["negative"] is True


def test_best_of_n_threshold_rises_with_grid_size():
    """★ 다중검정: 많이 찾아볼수록 우연히 좋은 게 나온다. 문턱이 그만큼 올라가야 한다."""
    d = np.random.default_rng(0).normal(0, 10, 5000)
    p1 = np.percentile(nm.best_of_n(d, 1, rounds=800), 95)
    p100 = np.percentile(nm.best_of_n(d, 100, rounds=800), 95)
    p1000 = np.percentile(nm.best_of_n(d, 1000, rounds=800), 95)
    assert p1 < p100 < p1000


def test_gate_can_pass_plain_null_but_fail_the_grid_corrected_one():
    """★ 이게 이 게이트를 넣은 이유다 — '귀무는 넘었지만 격자를 뒤져서 걸린 것'을 가른다."""
    d = np.random.default_rng(1).normal(0, 10, 5000)
    plain = float(np.percentile(d, 95))
    g = nm.gate(plain + 1.0, d, n_combos=500)
    assert g["beatsNull"] is True
    assert g["beatsBestOfN"] is False


def test_single_combo_needs_no_correction():
    d = np.random.default_rng(2).normal(0, 10, 3000)
    g = nm.gate(5.0, d, n_combos=1)
    assert g["bestOfNP95"] == g["nullP95"]


def test_matched_params_uses_actual_trades():
    """귀무 조건은 임의로 정하지 않고 **전략의 실제 트레이드**에서 뽑는다."""
    class T:
        def __init__(self, side, hold_min, lev):
            self.side, self.leverage = side, lev
            self.entry_time, self.exit_time = 0, hold_min * 60_000
            self.entry_price, self.qty = 100.0, 2.0

    class M:
        trades = [T(1, 45, 10), T(1, 45, 10), T(-1, 45, 10)]

    p = nm.matched_params(M(), "15m", initial_equity=1000.0)
    assert p["n_trades"] == 3 and p["hold_bars"] == 3      # 45분 / 15분봉
    assert p["side"] == "long" and p["leverage"] == 10
    assert abs(p["size_fraction"] - 0.02) < 1e-9          # 100*2/10 / 1000


def test_research_lib_shares_the_engine_implementation():
    """★ 판정이 두 곳에 각각 구현되면 언젠가 갈라진다 — 백테스트↔라이브에서 겪은 일이다."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import research.lib as L
    b = _base()
    a = L.null_model(b, "15m", 30, 8, "long", samples=200, seed=9)
    c = nm.simulate(b, 15, 30, 8, "long", samples=200, seed=9)
    assert np.allclose(a, c)
