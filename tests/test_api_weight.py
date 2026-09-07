"""IP 누적 weight 관측 — 밴 원인을 다음엔 추측하지 않기 위한 계측.

배경: -1003(IP 밴)을 두 번 맞고 두 번 다 원인을 못 밝혔다. 트레이더의 '요청 수'만 세고
있었는데 밴 기준은 **IP 단위 weight** 이고, 컬렉터는 ccxt 가 아니라 urllib 로 직접 쳐서
그 계측에 아예 안 잡혔다. 바이낸스가 헤더로 정답을 주므로 그걸 읽어 남긴다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import api_weight                                  # noqa: E402


def setup_function():
    api_weight.reset()


def test_header_is_case_insensitive():
    """헤더 대소문자는 클라이언트마다 다르다 — urllib 은 원본, ccxt 는 제각각."""
    assert api_weight.header_weight({"X-MBX-USED-WEIGHT-1M": "123"}) == 123
    assert api_weight.header_weight({"x-mbx-used-weight-1m": "45"}) == 45


def test_missing_header_is_zero_not_a_guess():
    """못 읽으면 0 = '이번엔 모른다'. 추정치를 지어내지 않는다."""
    assert api_weight.header_weight({}) == 0
    assert api_weight.header_weight(None) == 0
    assert api_weight.header_weight({"X-MBX-USED-WEIGHT-1M": "??"}) == 0


def test_zero_observation_does_not_clobber_peak():
    """★ '모름'(0)이 피크를 지우면 안 된다 — 밴 직전 값이 제일 중요한데 그게 날아간다."""
    d = tempfile.mkdtemp()
    api_weight.observe(900, service="trader", dir_path=d)
    api_weight.observe(0, service="trader", dir_path=d)
    assert api_weight.snapshot()["peak"] == 900


def test_peak_is_recorded_and_persisted():
    d = tempfile.mkdtemp()
    api_weight.observe(100, service="collector", dir_path=d)
    api_weight.observe(2000, service="collector", dir_path=d)
    api_weight.observe(300, service="collector", dir_path=d)
    with open(os.path.join(d, "collector.json"), encoding="utf-8") as f:
        rec = json.load(f)
    assert rec["peak"] == 2000 and rec["service"] == "collector"
    assert rec["limit"] == api_weight.LIMIT_1M


def test_read_all_sorts_by_peak():
    d = tempfile.mkdtemp()
    api_weight.observe(500, service="collector", dir_path=d)
    api_weight.reset()
    api_weight.observe(1800, service="trader", dir_path=d)
    rows = api_weight.read_all(d)
    assert [r["service"] for r in rows] == ["trader", "collector"]


def test_verdict_does_not_sum_services():
    """★ 헤더 값은 이미 **IP 합산**이다. 서비스별로 더하면 이중 계산이 된다."""
    rows = [{"peak": 1300}, {"peak": 1200}]
    v = api_weight.verdict(rows)
    assert "1300" in v and "2500" not in v


def test_verdict_warns_near_limit():
    assert "위험" in api_weight.verdict([{"peak": api_weight.LIMIT_1M // 2}])
    assert "여유" in api_weight.verdict([{"peak": 10}])


def test_verdict_says_unknown_when_never_observed():
    assert "관측 없음" in api_weight.verdict([])


def test_write_failure_never_raises():
    """관찰이 매매를 멈추면 안 된다 — 쓰기 불가 경로에서도 조용히 넘어간다."""
    api_weight.observe(700, service="trader", dir_path="/proc/nope/nowhere")
    assert api_weight.snapshot()["peak"] == 700
