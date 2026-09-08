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


# ---- _Guarded 를 실제로 통과시켜 본다 ----

class _FakeCCXT:
    """ccxt 표면 흉내 — 헤더를 남기는 것만 진짜처럼."""

    def __init__(self, weight):
        self.last_response_headers = {"X-MBX-USED-WEIGHT-1M": str(weight)}
        self.calls = []

    def fetch_balance(self, *a, **k):
        self.calls.append("fetch_balance")
        return {"USDC": {"total": 100.0}}

    def boom(self, *a, **k):
        raise RuntimeError("네트워크 실패")


class _FakeBrokerShell:
    def __init__(self, testnet=True):
        self.testnet, self.req_counts, self._banned_until = testnet, {}, 0

    def raise_if_banned(self):
        pass


def _guarded(weight, testnet=True):
    from engine.binance_broker import _Guarded
    ex = _FakeCCXT(weight)
    return _Guarded(ex, _FakeBrokerShell(testnet)), ex


def test_guarded_records_weight_with_the_endpoint_name():
    """★ 회귀: _record_weight 가 __getattr__ 의 지역변수 name 을 그냥 썼다 → 매 호출 NameError.

    except 가 그걸 조용히 삼켜, 테스트넷 계측이 통째로 죽은 채 배포됐다. 단위 테스트는
    api_weight 함수만 부르고 있어서 못 잡았다 — 래퍼를 실제로 통과시켜야 잡힌다.
    """
    api_weight.reset()
    g, _ = _guarded(700)
    g.fetch_balance()
    assert api_weight.snapshot(api_weight.TESTNET)["last"] == 700
    assert "fetch_balance" in api_weight.endpoints(api_weight.TESTNET)


def test_guarded_records_weight_even_when_the_call_fails():
    """밴 직전 응답이 제일 알고 싶은 값이다 — 실패해도 헤더는 남긴다(finally)."""
    api_weight.reset()
    g, _ = _guarded(2300)
    try:
        g.boom()
    except RuntimeError:
        pass
    assert api_weight.snapshot(api_weight.TESTNET)["last"] == 2300


def test_guarded_uses_mainnet_scope_when_not_testnet():
    api_weight.reset()
    g, _ = _guarded(120, testnet=False)
    g.fetch_balance()
    assert api_weight.snapshot(api_weight.MAINNET)["last"] == 120
    assert api_weight.snapshot(api_weight.TESTNET)["last"] == 0


def test_guarded_still_counts_requests():
    """계측을 넣다가 기존 요청 카운트를 깨뜨리지 않았는지."""
    api_weight.reset()
    g, _ = _guarded(50)
    g.fetch_balance()
    assert g._b.req_counts["fetch_balance"] == 1


# ---- 틀린 걸 아는 지표로 경보를 울리지 않는다 ----

def test_ccxt_source_is_excluded_from_verdict():
    """★ 2026-09-07: ccxt 값이 1806/2400 '위험'을 계속 띄웠는데 curl 실측은 1~2 였다.

    가짜 경보는 계측이 없느니만 못하다 — 그날 이 유령 하나를 쫓느라 시간을 썼다.
    기록은 남기되(원인 미해결) 판정에서는 뺀다.
    """
    rows = [{"scope": "testnet", "peak": 1806, "source": api_weight.CCXT},
            {"scope": "mainnet", "peak": 30, "source": api_weight.HTTP}]
    v = "\n".join(api_weight.verdict(rows))
    assert "1806" not in v and "위험" not in v
    assert "30" in v


def test_ccxt_rows_are_still_kept_for_debugging():
    """판정에서 뺀다고 지우지는 않는다 — 원인 규명이 아직 남아 있다."""
    rows = [{"scope": "testnet", "peak": 1806, "source": api_weight.CCXT}]
    assert api_weight.by_scope(rows, trusted_only=False) == {"testnet": 1806}
    assert "신뢰 가능한 weight 관측 없음" in "".join(api_weight.verdict(rows))


def test_guarded_marks_its_readings_as_ccxt():
    """DEFAULT_DIR 은 임포트 시점에 확정된다 — env 를 나중에 바꿔도 안 먹으므로 상수를 갈아끼운다."""
    api_weight.reset()
    d = tempfile.mkdtemp()
    orig = api_weight.DEFAULT_DIR
    api_weight.DEFAULT_DIR = d
    try:
        g, _ = _guarded(500)
        g.fetch_balance()
        name = f"{api_weight.service_name()}-testnet.json"
        with open(os.path.join(d, name), encoding="utf-8") as f:
            assert json.load(f)["source"] == api_weight.CCXT
    finally:
        api_weight.DEFAULT_DIR = orig


# ---- 불가능한 감소: 계측이 스스로 신뢰도를 판정한다 ----

def test_impossible_drop_marks_the_series_insane():
    """★ 2026-09-08 실측: 621 → 591. **1분 누계는 줄어들 수 없다.**

    줄었다면 그 값은 우리 IP 누계가 아니다(테스트넷 비공개 엔드포인트가 공유 카운터를
    돌려준다). 이 판단을 하루에 두 번 뒤집었고 두 번 다 근거가 '그럴 것 같다'였다 —
    반증 가능한 성질 하나를 코드가 직접 검사하게 둔다.
    """
    api_weight.reset()
    d = tempfile.mkdtemp()
    api_weight.observe(621, scope="testnet", service="t", dir_path=d, now=1000.0)
    api_weight.observe(591, scope="testnet", service="t", dir_path=d, now=1002.0)
    assert api_weight.snapshot("testnet")["sane"] is False


def test_minute_rollover_is_not_a_drop():
    """분이 바뀌면 0 근처로 리셋된다 — 그건 감소가 아니다(정상 계열을 죽이면 안 된다)."""
    api_weight.reset()
    d = tempfile.mkdtemp()
    api_weight.observe(2000, scope="mainnet", service="t", dir_path=d, now=1000.0)
    api_weight.observe(3, scope="mainnet", service="t", dir_path=d, now=1002.0)
    assert api_weight.snapshot("mainnet")["sane"] is True


def test_insane_series_is_persisted_immediately():
    """★ 상태 전이를 바로 안 쓰면 파일이 계속 sane=True 라 판정에서 안 빠진다."""
    api_weight.reset()
    d = tempfile.mkdtemp()
    api_weight.observe(621, scope="testnet", service="t", dir_path=d, now=1000.0)
    api_weight.observe(591, scope="testnet", service="t", dir_path=d, now=1002.0)
    with open(os.path.join(d, "t-testnet.json"), encoding="utf-8") as f:
        assert json.load(f)["sane"] is False


def test_insane_series_is_excluded_from_verdict():
    rows = [{"scope": "testnet", "peak": 4444, "source": api_weight.HTTP, "sane": False},
            {"scope": "mainnet", "peak": 30, "source": api_weight.HTTP, "sane": True}]
    v = "\n".join(api_weight.verdict(rows))
    assert "4444" not in v and "30" in v


# ---- 주문수: weight 와 별개 한도 ----

def test_order_count_is_tracked_separately():
    """weight 가 여유여도 주문수에서 밴이 난다 — maker 추격은 넣고 취소를 반복한다."""
    api_weight.reset()
    d = tempfile.mkdtemp()
    api_weight.observe(10, orders=800, scope="testnet", service="t", dir_path=d)
    st = api_weight.snapshot("testnet")
    assert st["peak"] == 10 and st["orderPeak"] == 800


def test_order_count_appears_in_verdict_with_its_own_limit():
    v = "\n".join(api_weight.verdict([{"scope": "mainnet", "peak": 30, "orderPeak": 700}]))
    assert f"700/{api_weight.ORDER_LIMIT_1M}" in v and "위험" in v


def test_order_count_counted_even_from_untrusted_source():
    """★ weight 경로가 미검증이어도 주문수는 센다 — 주문 헤더는 그 문제와 무관하다."""
    v = "\n".join(api_weight.verdict(
        [{"scope": "testnet", "peak": 4444, "source": api_weight.CCXT, "orderPeak": 900}]))
    assert "4444" not in v and "900" in v


def test_guarded_records_order_count():
    api_weight.reset()
    from engine.binance_broker import _Guarded
    ex = _FakeCCXT(12)
    ex.last_response_headers["X-MBX-ORDER-COUNT-1M"] = "455"
    _Guarded(ex, _FakeBrokerShell(True)).fetch_balance()
    assert api_weight.snapshot(api_weight.TESTNET)["orderPeak"] == 455
