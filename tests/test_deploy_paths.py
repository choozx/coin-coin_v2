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


def test_prod_dashboard_serves_every_api_its_pages_call():
    """프로덕션 대시보드(engine.dashboard)가 자기 페이지들이 부르는 /api/* 를 전부 제공해야 한다.

    왜: 프론트엔드(dashboard.html·collector.html)는 로컬 스튜디오(engine.server)와 **공용**인데,
    server.py 는 프로덕션 이미지에서 제외된다(.dockerignore). 그래서 새 기능을 server.py 에만
    배선하면 로컬에선 멀쩡하고 **프로덕션에서만** 깨진다 — 게다가 조용히 깨지지도 않고
    "not found" 를 JSON.parse 하다 죽는 식이라 원인 찾기도 나쁘다.

    실제로 이 방식으로 세 번 샜다: /collector 페이지, /api/bot-config, /api/settings.
    (마지막 둘은 리스크 가드레일 설정 — 프로덕션에서 킬스위치를 켤 수 없었다.)
    """
    import re
    dash_py = (ENGINE / "dashboard.py").read_text(encoding="utf-8")
    served = set(re.findall(r'"(/api/[a-z_-]+)"', dash_py))
    for page in ("dashboard.html", "collector.html"):
        called = set(re.findall(r'["\'`](/api/[a-z_-]+)', (ENGINE / page).read_text(encoding="utf-8")))
        missing = called - served
        assert not missing, (
            f"[{page}] 가 부르는데 engine/dashboard.py 에 없는 API: {sorted(missing)} — "
            f"프로덕션에서 이 화면이 깨진다(server.py 는 프로덕션 이미지에 없음). "
            f"dashboard.py 에 라우트를 추가할 것.")


def test_prod_dashboard_has_no_dead_page_links():
    """프로덕션이 서빙하는 페이지의 내부 링크가 전부 살아 있어야 한다.

    dashboard.html·collector.html 은 로컬 스튜디오와 공용이라 /backtest 같은 '로컬에만 있는'
    링크를 달고 있다. 프로덕션에서 그건 404 — 사용자가 클릭해봐야 알게 된다.
    허용되는 예외는 딱 하나: id="studioLink" 를 달아 __NO_STUDIO__ 플래그로 숨기는 링크.
    (실제로 dashboard.html 만 숨기고 collector.html 은 빠뜨려서 한 번 샜다.)
    """
    import re
    dash_py = (ENGINE / "dashboard.py").read_text(encoding="utf-8")
    for page in ("dashboard.html", "collector.html"):
        html = (ENGINE / page).read_text(encoding="utf-8")
        for m in re.finditer(r'<a\s+href="(/[^"#]*)"([^>]*)>', html):
            href, attrs = m.group(1), m.group(2)
            path = "/" + href.lstrip("/").split("?")[0].rstrip("/")
            if path == "/":
                continue                                  # 랜딩은 항상 있다
            if 'id="studioLink"' in attrs:                # 프로덕션에서 숨기는 링크
                assert "__NO_STUDIO__" in html, (
                    f"[{page}] studioLink 는 있는데 숨기는 스크립트가 없다 — 죽은 링크가 노출된다")
                continue
            assert f'"{path}"' in dash_py, (
                f"[{page}] 의 링크 {href} 를 engine/dashboard.py 가 서빙하지 않는다 — "
                f"프로덕션에서 404. 라우트를 추가하거나 id=\"studioLink\" 로 숨길 것.")


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
            passed += 1
    print(f"\n{passed}/{passed} passed")
