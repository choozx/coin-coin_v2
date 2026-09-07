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
    with open(os.path.join(d, "collector-mainnet.json"), encoding="utf-8") as f:
        rec = json.load(f)
    assert rec["peak"] == 2000 and rec["service"] == "collector"
    assert rec["scope"] == api_weight.MAINNET and rec["limit"] == api_weight.LIMIT_1M


def test_read_all_sorts_by_peak():
    d = tempfile.mkdtemp()
    api_weight.observe(500, service="collector", dir_path=d)
    api_weight.reset()
    api_weight.observe(1800, service="trader", dir_path=d)
    rows = api_weight.read_all(d)
    assert [r["service"] for r in rows] == ["trader", "collector"]


# ---- scope: 같은 프로세스가 두 호스트를 친다 ----

def test_scopes_are_separate_counters():
    """★ 트레이더는 주문을 테스트넷으로, 캔들을 메인넷으로 친다 — **별개 카운터**다.

    처음엔 한 통에 섞어 담았고, 그래서 '트레이더 1407 vs 컬렉터 20' 이 모순처럼 보였다.
    어느 호스트 것인지 모르는 숫자는 계측이 아니다.
    """
    d = tempfile.mkdtemp()
    api_weight.observe(1400, scope=api_weight.TESTNET, service="trader", dir_path=d)
    api_weight.observe(30, scope=api_weight.MAINNET, service="trader", dir_path=d)
    assert api_weight.snapshot(api_weight.TESTNET)["peak"] == 1400
    assert api_weight.snapshot(api_weight.MAINNET)["peak"] == 30
    assert sorted(os.listdir(d)) == ["trader-mainnet.json", "trader-testnet.json"]


def test_by_scope_never_merges_hosts():
    rows = [{"scope": "testnet", "peak": 1400}, {"scope": "mainnet", "peak": 30}]
    assert api_weight.by_scope(rows) == {"testnet": 1400, "mainnet": 30}


def test_verdict_does_not_sum_services_within_a_scope():
    """★ 같은 scope 안에선 헤더가 이미 IP 합산이다. 서비스별로 더하면 이중 계산이 된다."""
    rows = [{"scope": "mainnet", "peak": 1300}, {"scope": "mainnet", "peak": 1200}]
    v = "\n".join(api_weight.verdict(rows))
    assert "1300" in v and "2500" not in v


def test_verdict_reports_each_scope_separately():
    rows = [{"scope": "testnet", "peak": 1400}, {"scope": "mainnet", "peak": 30}]
    v = api_weight.verdict(rows)
    assert len(v) == 2
    assert any("testnet" in l and "1400" in l for l in v)
    assert any("mainnet" in l and "30" in l for l in v)


def test_verdict_warns_near_limit():
    hot = "\n".join(api_weight.verdict([{"peak": api_weight.LIMIT_1M // 2}]))
    cool = "\n".join(api_weight.verdict([{"peak": 10}]))
    assert "위험" in hot and "여유" in cool


def test_verdict_says_unknown_when_never_observed():
    assert "관측 없음" in "".join(api_weight.verdict([]))


def test_write_failure_never_raises():
    """관찰이 매매를 멈추면 안 된다 — 쓰기 불가 경로에서도 조용히 넘어간다."""
    api_weight.observe(700, service="trader", dir_path="/proc/nope/nowhere")
    assert api_weight.snapshot()["peak"] == 700


# ---- 엔드포인트 귀속: '누가 쓰는가' ----

def test_charge_attributes_the_increase_to_the_call():
    """호출 직전/직후 차이를 그 엔드포인트에 귀속시킨다 — 값 하나로는 범인을 못 가린다."""
    d = tempfile.mkdtemp()
    api_weight.charge("fetch_balance", 100, scope="testnet", dir_path=d, now=1000.0)
    api_weight.charge("fetch_positions", 105, scope="testnet", dir_path=d, now=1001.0)
    eps = api_weight.endpoints("testnet")
    assert eps["fetch_positions"]["max"] == 5


def test_charge_ignores_the_minute_rollover():
    """★ 카운터는 매 분 0 으로 리셋된다. 그 경계의 음수 차이를 비용으로 세면 안 된다."""
    d = tempfile.mkdtemp()
    api_weight.charge("a", 2000, scope="testnet", dir_path=d, now=1000.0)
    got = api_weight.charge("a", 12, scope="testnet", dir_path=d, now=1061.0)   # 다음 분
    assert got == 0
    assert api_weight.endpoints("testnet")["a"]["max"] == 0


def test_charge_still_tracks_peak():
    """귀속을 넣어도 피크 관측은 그대로여야 한다."""
    d = tempfile.mkdtemp()
    api_weight.charge("a", 300, scope="testnet", dir_path=d)
    api_weight.charge("b", 90, scope="testnet", dir_path=d)
    assert api_weight.snapshot("testnet")["peak"] == 300


def test_read_all_drops_pre_scope_records():
    """★ scope 도입 전 파일은 테스트넷·메인넷이 섞인 값이다 — 보여주면 없는 위험을 만든다.

    실제로 배포 직후 옛 trader.json 이 남아 '메인넷 1791/2400 위험'이라는 유령이 나왔다.
    """
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "trader.json"), "w", encoding="utf-8") as f:
        json.dump({"service": "trader", "peak": 1791, "last": 20}, f)     # scope 없음
    api_weight.charge("x", 30, scope="mainnet", service="trader", dir_path=d)
    rows = api_weight.read_all(d)
    assert [r["peak"] for r in rows] == [30]


def test_charge_is_written_in_the_same_call_not_one_behind():
    """★ 귀속을 observe 뒤에 하면 기록이 한 박자 뒤처져, 방금 비싼 호출이 파일에 안 들어간다."""
    d = tempfile.mkdtemp()
    api_weight.charge("cheap", 100, scope="testnet", service="trader", dir_path=d, now=1000.0)
    api_weight.charge("expensive", 900, scope="testnet", service="trader", dir_path=d, now=1001.0)
    with open(os.path.join(d, "trader-testnet.json"), encoding="utf-8") as f:
        rec = json.load(f)
    assert rec["endpoints"]["expensive"]["max"] == 800
