"""사이징 하드 상한 — 레버리지·자본비중 천장이 오설정을 클램프하는지 검증.

실돈 봇에서 leverage=125 / equityPercent=100 같은 재앙적 설정이 한 방에 계좌를 날리지 않도록,
_leverage_for 는 max_lev 로, _open_position 은 equity×max_account_fraction 으로 클램프해야 한다.
백테스트 기본값(125 / 1.0)에선 동작이 안 바뀌어야 한다(라이브만 settings 로 타이트하게 주입).
"""
from __future__ import annotations

import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import settings                                # noqa: E402
from engine.backtest import BacktestConfig, _leverage_for, _open_position   # noqa: E402


def _open(sizing, cfg, lev=10, equity=1000.0, price=100.0):
    # ex={} → 손절/익절 없음(atr·signal 미사용 경로). preset/signal 은 이 경로에서 안 쓰임.
    return _open_position(None, sizing, {}, price, 0, 0, 1, lev, equity, cfg,
                          np.array([float("nan")]), None)


# ---- 자본비중 상한 (_open_position) ----

def test_account_fraction_clamps_margin():
    """equityPercent=100 이라도 max_account_fraction=0.9 면 증거금이 잔고의 90%로 제한."""
    sizing = {"size": {"type": "equityPercent", "value": 100}}
    p = _open(sizing, BacktestConfig(max_account_fraction=0.9))
    assert abs(p.margin - 900.0) < 1e-6                    # 1000×0.9
    assert abs(p.qty - 900.0 * 10 / 100.0) < 1e-6          # qty 도 함께 재계산


def test_account_fraction_default_unchanged():
    """기본값 1.0 = 현행(100%까지). 백테스트 결과 불변 보장."""
    sizing = {"size": {"type": "equityPercent", "value": 100}}
    p = _open(sizing, BacktestConfig())                    # max_account_fraction=1.0
    assert abs(p.margin - 1000.0) < 1e-6


def test_account_fraction_no_effect_when_under_cap():
    """정상 비중(10%)은 상한(90%)에 안 걸린다."""
    sizing = {"size": {"type": "equityPercent", "value": 10}}
    p = _open(sizing, BacktestConfig(max_account_fraction=0.9))
    assert abs(p.margin - 100.0) < 1e-6                    # 1000×0.1, 클램프 안 됨


# ---- 레버리지 상한 (_leverage_for) ----

def test_leverage_clamped_to_max():
    assert _leverage_for({"leverage": 125}, 1000.0, 25) == 25   # 초과 → 클램프
    assert _leverage_for({"leverage": 10}, 1000.0, 25) == 10    # 이하 → 그대로


def test_leverage_tiers_also_clamped():
    tiers = {"leverageTiers": [{"maxBalance": None, "leverage": 100}]}
    assert _leverage_for(tiers, 1000.0, 25) == 25               # 동적 티어도 상한 적용


# ---- settings 가드레일 기본값·병합 ----

def test_guardrail_sizing_caps_default():
    p = os.path.join(tempfile.mkdtemp(), "none.json")           # 파일 없음 → 기본값
    g = settings.get_guardrails(path=p)
    assert g["maxLeverage"] == 25 and g["maxAccountFractionPct"] == 90.0


def test_guardrail_sizing_caps_override_and_validate():
    p = os.path.join(tempfile.mkdtemp(), "s.json")
    settings.set_guardrails({"maxLeverage": 5, "maxAccountFractionPct": 50}, path=p)
    g = settings.get_guardrails(path=p)
    assert g["maxLeverage"] == 5 and g["maxAccountFractionPct"] == 50.0
    # 잘못된 값은 무시하고 기본 유지
    settings.set_guardrails({"maxLeverage": "abc", "maxAccountFractionPct": 999}, path=p)
    g2 = settings.get_guardrails(path=p)
    assert g2["maxLeverage"] == 25                             # 파싱 실패 → 기본
    assert g2["maxAccountFractionPct"] == 100.0                # 100 초과는 100으로 클램프


if __name__ == "__main__":
    import traceback
    fns = [x for k, x in sorted(globals().items()) if k.startswith("test_") and callable(x)]
    passed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}"); passed += 1
        except Exception:
            print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{passed}/{len(fns)} passed")
    sys.exit(0 if passed == len(fns) else 1)
