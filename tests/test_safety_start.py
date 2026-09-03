"""기동 시 매매 상태 결정 — 안전 멈춤 vs 의도 보존(네트워크·디스코드 없음).

배경: 재배포로 트레이더 컨테이너가 교체되자 사용자의 '재개'가 조용히 취소돼 봇이 며칠간
매매를 안 했다. 봇에게 그 뒤로는 '처음부터 멈춤'과 구별되지 않아(_sync_paused 전이 없음)
알림도 안 나갔다. 그래서 ① 덮어쓸 땐 알리고 ② 가짜돈이면 의도를 이어받는다.
메인넷은 예외 없이 멈춤 — 나쁜 배포가 즉시 실주문을 내면 안 된다.

★ 판정 기준은 '권한'(--real-money)이 아니라 '이번 기동이 붙는 네트워크'다. 권한으로 재면
대시보드에서 네트워크를 오가려고 --real-money 를 준 **테스트넷 봇까지 배포마다 꺼진다**
— 실제로 그렇게 됐고 아무도 몰랐다.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import control                      # noqa: E402
from engine.live import safety_pause_on_start   # noqa: E402


def _tmp():
    return os.path.join(tempfile.mkdtemp(), "control.json")


def _start(path, real_money=False, start_running=False, once=False, live=True, testnet=True):
    return safety_pause_on_start(start_running, once, live, real_money, path=path, testnet=testnet)


# ---- 가짜돈: 의도를 이어받는다 ----

def test_paper_resumes_user_intent():
    """사용자가 재개해 둔 페이퍼/테스트넷 봇은 재시작 후에도 계속 매매한다."""
    p = _tmp()
    control.set_service("trader", "running", p)          # 대시보드에서 재개(의도 기록됨)
    assert _start(p) == "resumed"
    assert control.service_state("trader", p) == "running"


def test_paper_stays_paused_when_user_paused():
    """사용자가 멈춰 둔 봇은 재시작해도 멈춤 그대로 — 의도를 존중한다."""
    p = _tmp()
    control.set_service("trader", "paused", p)
    assert _start(p) == "kept"
    assert control.service_state("trader", p) == "paused"


def test_first_boot_never_auto_resumes():
    """control.json 이 없는 최초 기동엔 의도 기록이 없다 → 자동 재개하지 않는다(안전측)."""
    p = _tmp()
    assert _start(p) == "kept"
    assert control.service_state("trader", p) == "paused"


# ---- 메인넷: 예외 없이 멈춤 ----

def test_mainnet_always_pauses_and_reports():
    """메인넷 봇은 의도가 running 이어도 멈춤으로 시작하고, 덮어썼음을 알린다."""
    p = _tmp()
    control.set_service("trader", "running", p)
    assert _start(p, real_money=True, testnet=False) == "overwrote"
    assert control.service_state("trader", p) == "paused"


def test_mainnet_does_not_spam_on_repeat_restart():
    """이미 멈춰 있으면 조용하다 — 재시작마다 같은 경고를 반복하지 않는다."""
    p = _tmp()
    control.set_service("trader", "running", p)
    assert _start(p, real_money=True, testnet=False) == "overwrote"
    assert _start(p, real_money=True, testnet=False) == "kept"   # 두 번째부터는 침묵


def test_testnet_resumes_even_with_real_money_permission():
    """★ 실돈 '권한'이 있어도 지금 붙는 곳이 테스트넷이면 가짜돈이다 → 이어받는다.

    이 한 줄이 없어서 테스트넷 봇이 배포마다 꺼졌다. 권한은 '메인넷에 갈 수 있다'는 뜻이지
    '지금 메인넷이다'가 아니다.
    """
    p = _tmp()
    control.set_service("trader", "running", p)
    assert _start(p, real_money=True, testnet=True) == "resumed"
    assert control.service_state("trader", p) == "running"


# ---- 의도 기록의 경계 ----

def test_startup_pause_does_not_clobber_intent():
    """기동 안전 멈춤은 의도를 건드리지 않는다 — 안 그러면 자동 재개가 영영 안 산다."""
    p = _tmp()
    control.set_service("trader", "running", p)
    _start(p, real_money=True, testnet=False)            # 메인넷 기동 → trader=paused
    assert control.trader_intent(p) == "running"         # 그러나 사용자 의도는 그대로
    assert _start(p) == "resumed"                        # 가짜돈으로 다시 뜨면 이어받는다


def test_flatten_autopause_clears_intent():
    """즉시청산 후 자동 정지는 '사용자가 멈추길 원했다' → 재시작해도 자동 재개 안 됨."""
    p = _tmp()
    control.set_service("trader", "running", p)
    control.set_service("trader", "paused", p)           # live._maybe_flatten 과 같은 호출
    assert control.trader_intent(p) == "paused"
    assert _start(p) == "kept"


# ---- 안전 멈춤을 건너뛰는 경로 ----

def test_start_running_skips_entirely():
    """--start-running 은 상태를 손대지 않는다(수집기가 쓰는 경로)."""
    p = _tmp()
    control.set_service("trader", "running", p)
    assert _start(p, start_running=True) == "skip"
    assert control.service_state("trader", p) == "running"


def test_once_paper_skips_but_once_live_does_not():
    """--once 는 페이퍼에선 그냥 돌게 두고, 실거래면 예외 없이 멈춤 판정을 거친다."""
    p = _tmp()
    control.set_service("trader", "paused", p)
    assert _start(p, once=True, live=False) == "skip"
    assert _start(p, once=True, live=True) == "kept"


# ---- 연속 손실 기준선 저장 (settings.json) ----

def test_streak_reset_roundtrip():
    """기준선은 설정(guardrails)이 아니라 상태라 키를 분리해 저장한다 — 서로 안 덮어쓴다."""
    from engine import settings
    sp = os.path.join(tempfile.mkdtemp(), "settings.json")
    assert settings.get_streak_reset_ms(sp) == 0          # 없으면 0 = 전체 이력을 본다
    settings.set_guardrails({"killSwitch": True}, sp)
    settings.set_streak_reset_ms(1_700_000_000_000, sp)
    assert settings.get_streak_reset_ms(sp) == 1_700_000_000_000
    assert settings.get_guardrails(sp)["killSwitch"] is True
    settings.set_guardrails({"killSwitch": False}, sp)    # 설정 저장이 기준선을 날리지 않는다
    assert settings.get_streak_reset_ms(sp) == 1_700_000_000_000


def test_cooldown_default_is_on():
    """기본값이 자동 해제여야 한다 — 기본이 래치면 같은 사고가 반복된다."""
    from engine import settings
    sp = os.path.join(tempfile.mkdtemp(), "settings.json")
    assert settings.get_guardrails(sp)["maxConsecutiveLosses"]["cooldownHours"] > 0
