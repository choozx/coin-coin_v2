"""줄 단위 JSON 로그(JSONL) 공용 배관 — 관찰용 기록의 append/보관/읽기.

진입 판정(entry_log)과 체결 실측(fill_log)이 같은 요구를 갖는다: 한 줄씩 쌓되 무한히 자라면
안 되고(저사양 EC2), **어떤 실패도 매매를 막으면 안 된다**. 같은 코드를 두 벌 두면 트리밍
같은 미묘한 부분이 한쪽만 고쳐진다 — 실제로 그럴 뻔했다. 그래서 여기 한 곳에만 둔다.
"""
from __future__ import annotations

import json
import os

_since_check: dict = {}          # path → 마지막 점검 이후 append 수


def append(rec: dict, path: str, keep: int) -> None:
    """한 줄 추가. keep 를 넘으면 오래된 줄을 버린다. keep=0 이면 무제한, path 가 비면 기록 끔.

    실패해도 절대 예외를 올리지 않는다 — 관찰용이지 매매 경로가 아니다.
    로그를 못 써서 봇이 서는 일은 없어야 한다.
    """
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if keep and _due_for_trim(path, keep) and _too_long(path, keep):
            _trim(path, keep)
    except Exception:
        pass


def _due_for_trim(path: str, keep: int) -> bool:
    """줄 수 세기는 파일 전체를 읽는 일이다 — 매 append 마다 하면 로그가 길수록 느려진다.

    점검 주기를 keep 의 절반(최대 200)으로 잡는다: 그래야 다음 점검 전까지 늘어나는 양이
    _too_long 의 여유(keep*0.5)를 넘지 않아 파일이 keep*1.5 안에 머문다.
    """
    every = max(1, min(200, keep // 2))
    n = _since_check.get(path, every) + 1        # 첫 호출은 바로 점검(재시작 직후 정리)
    if n >= every:
        _since_check[path] = 0
        return True
    _since_check[path] = n
    return False


def _too_long(path: str, keep: int) -> bool:
    with open(path, "rb") as f:
        n = sum(1 for _ in f)
    return n > keep * 3 // 2


def _trim(path: str, keep: int) -> None:
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines[-keep:])
    os.replace(tmp, path)


def tail(path: str, n: int = 50) -> list:
    """최근 n 줄을 dict 로. 파일이 없거나 못 읽으면 빈 리스트(깨진 줄은 건너뛴다)."""
    if not path:
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out
