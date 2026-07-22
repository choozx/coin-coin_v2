"""deploy/service-deps.conf 가 engine/ 의 실제 import 그래프와 일치하는지 검사.

왜 필요한가: 배포 폴러(deploy/pull-deploy.sh)는 이 매핑을 보고 "이 커밋은 대시보드만
바뀌었으니 매매 봇은 안 건드린다"를 판단한다. 매핑이 낡으면 **트레이더가 옛 코드로 계속
도는데 배포가 성공했다고 표시된다** — 조용히 틀리는 종류의 버그라 테스트로 막는다.

engine/live.py 가 새 모듈을 import 하기 시작하면 이 테스트가 먼저 깨진다.
그러면 deploy/service-deps.conf 의 trader_mods 에 그 모듈을 추가할 것.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine"
CONF = ROOT / "deploy" / "service-deps.conf"

ENTRY = {"trader": "live", "collector": "collector", "dashboard": "dashboard"}


def _local_modules() -> dict[str, pathlib.Path]:
    return {p.stem: p for p in ENGINE.glob("*.py")}


def _closure(entry: str) -> set[str]:
    """entry 모듈이 (간접 포함) 끌어오는 engine/ 내부 모듈 전체."""
    mods = _local_modules()
    seen: set[str] = set()

    def walk(name: str) -> None:
        if name in seen or name not in mods:
            return
        seen.add(name)
        for node in ast.walk(ast.parse(mods[name].read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                # `from . import x` / `from .mod import y` / `from engine.mod import y`
                if node.level:                       # 상대 임포트
                    if node.module:
                        walk(node.module.split(".")[0])
                    for a in node.names:
                        walk(a.name)
                elif node.module and node.module.startswith("engine"):
                    parts = node.module.split(".")
                    if len(parts) > 1:
                        walk(parts[1])
                    for a in node.names:
                        walk(a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("engine."):
                        walk(a.name.split(".")[1])

    walk(entry)
    return seen


def _conf_mods(service: str) -> set[str]:
    for line in CONF.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{service}_mods="):
            return set(line.split("=", 1)[1].split())
    raise AssertionError(f"{CONF.name}: {service}_mods 항목이 없음")


def test_service_deps_conf_matches_imports():
    """선언된 모듈 목록 == 실제 import 폐포."""
    for service, entry in ENTRY.items():
        actual = _closure(entry)
        declared = _conf_mods(service)
        missing = actual - declared
        extra = declared - actual
        assert not missing, (
            f"[{service}] service-deps.conf 에 빠진 모듈: {sorted(missing)} — "
            f"이 파일이 바뀌어도 {service} 컨테이너가 교체되지 않아 옛 코드로 계속 돈다. "
            f"deploy/service-deps.conf 의 {service}_mods 에 추가할 것."
        )
        assert not extra, (
            f"[{service}] service-deps.conf 에만 있고 실제로는 안 쓰는 모듈: {sorted(extra)} — "
            f"불필요한 재시작을 유발한다. 제거할 것."
        )


def test_every_engine_module_is_covered():
    """engine/*.py 중 어느 서비스에도 안 잡히는 모듈이 없어야 한다(신규 파일 누락 방지)."""
    covered = set().union(*(_conf_mods(s) for s in ENTRY))
    orphans = set(_local_modules()) - covered - {"__init__"}
    assert not orphans, (
        f"어느 서비스에도 속하지 않는 모듈: {sorted(orphans)} — 새로 추가했다면 "
        f"deploy/service-deps.conf 의 해당 서비스 목록에 넣을 것(어디에도 안 넣으면 "
        f"그 파일만 고친 배포는 아무 컨테이너도 교체하지 않는다)."
    )


def _conf_extra_patterns() -> list[str]:
    """모든 *_extra 정규식(서비스별 + all_extra)."""
    out = []
    for line in CONF.read_text(encoding="utf-8").splitlines():
        if "_extra=" in line and not line.startswith("#"):
            pat = line.split("=", 1)[1].strip()
            if pat:
                out.append(pat)
    return out


def test_every_frontend_file_is_covered():
    """engine/*.html 도 어느 *_extra 에는 잡혀야 한다.

    모듈(.py)만 검사하다 collector.html 이 dashboard_extra 에서 빠진 채 지나갔다 —
    UI를 고쳐도 대시보드 컨테이너가 안 바뀌어 '배포 성공'인데 화면은 옛것.
    """
    import re
    pats = [re.compile(p) for p in _conf_extra_patterns()]
    orphans = [f"engine/{p.name}" for p in sorted(ENGINE.glob("*.html"))
               if not any(rx.search(f"engine/{p.name}") for rx in pats)]
    assert not orphans, (
        f"어느 *_extra 에도 안 잡히는 프론트엔드 파일: {orphans} — 이 파일만 고친 배포는 "
        f"아무 컨테이너도 교체하지 않는다. deploy/service-deps.conf 의 해당 *_extra 에 추가할 것."
    )


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
            passed += 1
    print(f"\n{passed}/{passed} passed")
