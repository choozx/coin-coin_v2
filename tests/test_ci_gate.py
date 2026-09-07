"""CI 테스트 게이트가 **실제로** 전부 돌리는가.

2026-09-07: CI 는 `for f in tests/test_*.py; do python "$f"; done` 로 돌고 있었다.
__main__ 러너가 없는 파일은 아무것도 실행하지 않고 **0 으로 통과**한다 — 12개 파일이
그 상태였다(로컬 pytest 292개 vs CI 136개). 이번 주 내내 넣은 회귀 테스트들이 CI 에선
한 번도 안 돌았다. 게이트가 있는 척만 하고 있었다.

pytest 수집으로 바꿨고, 그 방식이 되돌아가지 않게 여기서 잠근다.
"""
from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "deploy.yml")


def _workflow() -> str:
    with open(WORKFLOW, encoding="utf-8") as f:
        return f.read()


def _commands() -> str:
    """주석을 뺀 실행부만. 주석에 옛 방식을 설명해 뒀는데 그것까지 잡으면 오탐이다."""
    return "\n".join(l for l in _workflow().splitlines() if not l.lstrip().startswith("#"))


def test_ci_runs_pytest_over_the_whole_suite():
    """★ 파일 단위 루프로 되돌아가면 조용히 일부만 도는 게이트가 된다."""
    w = _commands()
    assert "pytest tests/" in w, "CI 가 pytest 로 tests/ 를 수집해야 한다"


def test_ci_does_not_loop_over_files_with_own_runners():
    """옛 방식으로 되돌아가면 안 된다 — 러너 없는 파일을 조용히 건너뛴다."""
    w = _commands()
    assert not re.search(r"for f in tests/test_\*\.py", w), \
        "파일 단위 루프는 __main__ 러너가 없는 파일을 조용히 건너뛴다"


def test_build_still_requires_tests_to_pass():
    """테스트가 깨지면 이미지가 안 나가야 한다 — 이게 게이트의 존재 이유다."""
    w = _commands()
    assert "needs: test" in w
