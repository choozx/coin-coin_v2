"""XS MOM 원격 명령 — 디스코드 봇이 요청 파일을 남기고, xsbot 이 1분 안에 읽어 실행한다.

디스코드 봇(discordbot)엔 바이낸스 키가 없다 → 주문은 지금처럼 xsbot 하나만 낸다.
두 컨테이너가 같은 data 볼륨을 쓰므로 $XS_DIR/cmd/ 에 파일 하나 = 명령 하나(요일 분산 켜기 split_on 과 같은 방식).
  · 실행 전에 파일부터 지운다 → 같은 명령이 두 번 실행되지 않는다(실행 중 죽으면 그 명령은 버려지고 알림이 안 온다).
  · 10분 넘게 처리 못 한 명령(xsbot 이 꺼져 있었다 등)은 실행하지 않고 '만료'로 알린다 — 재시작 뒤 뜻밖의 청산 방지.
  · 결과는 xsbot 이 #매매 에 올린다. 기록은 $XS_DIR/cmd_log.jsonl.
여기엔 표준 라이브러리만 — 디스코드 봇과 xsbot 둘 다 import 한다.
"""
from __future__ import annotations

import json
import os
import time
import uuid

CMDS = {
    "compare": "실매매 ↔ 규칙 대조",
    "resume": "정지 해제(다음 리밸런스부터)",
    "resume_go": "정지 해제 + 지금 진입",
    "flatten": "XS 포지션 전량 청산·정지",
    "rebase": "손실 한도 기준을 지금 잔고로",
    "split_on": "요일 분산 켜기(되돌리지 않음)",
}
TTL_MS = 10 * 60_000


def xs_dir():
    return os.environ.get("XS_DIR", "data/xs")


def cmd_dir():
    return os.path.join(xs_dir(), "cmd")


def request(cmd, who=None, now_ms=None):
    """명령 요청 파일을 남긴다(원자적: 임시 파일 → 이름 바꾸기). 반환: 파일 이름."""
    if cmd not in CMDS:
        raise ValueError(f"모르는 명령 {cmd!r}")
    now = int(now_ms or time.time() * 1000)
    d = cmd_dir()
    os.makedirs(d, exist_ok=True)
    name = f"{now:013d}-{cmd}-{uuid.uuid4().hex[:6]}.json"
    tmp = os.path.join(d, "." + name)
    with open(tmp, "w") as f:
        json.dump(dict(cmd=cmd, at=now, who=who), f, ensure_ascii=False)
    os.replace(tmp, os.path.join(d, name))
    return name


def take(now_ms=None):
    """쌓인 명령을 오래된 순으로 꺼낸다(파일은 지움). 각 항목에 expired(10분 넘음) · error(못 읽음) 표시."""
    now = int(now_ms or time.time() * 1000)
    d = cmd_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d)):
        if name.startswith(".") or not name.endswith(".json"):
            continue
        p = os.path.join(d, name)
        try:
            with open(p) as f:
                req = json.load(f)
        except Exception as e:      # noqa: BLE001 — 깨진 파일 하나가 나머지를 막으면 안 된다
            req = dict(cmd=None, error=f"{type(e).__name__}: {e}", file=name)
        try:
            os.remove(p)
        except FileNotFoundError:   # 다른 쪽이 먼저 가져갔다
            continue
        if req.get("cmd") not in CMDS and not req.get("error"):
            req["error"] = f"모르는 명령 {req.get('cmd')!r}"
        req["expired"] = bool(req.get("at")) and now - int(req["at"]) > TTL_MS
        out.append(req)
    return out


def log(rec):
    os.makedirs(xs_dir(), exist_ok=True)
    with open(os.path.join(xs_dir(), "cmd_log.jsonl"), "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_status():
    """xsbot 이 5분마다 쓰는 status.json(대시보드와 같은 것). 없거나 깨지면 {'error': ...}."""
    try:
        with open(os.path.join(xs_dir(), "status.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"error": "status.json 없음 — xsbot 이 아직 안 돌았어요"}
    except Exception as e:      # noqa: BLE001
        return {"error": f"status.json 못 읽음: {e}"}


def _usd(x):
    return "—" if x is None else f"${x:,.2f}"


def status_text(d):
    """status.json → 디스코드 한 화면(2,000자 안)."""
    if d.get("error") and "mode" not in d:
        return f"⚠️ {d['error']}"
    mode = d.get("mode")
    head = "⛔ **정지**" + (f" — {d.get('halt_reason')}" if d.get("halt_reason") else "") if d.get("halted") else "🟢 운영 중"
    lines = [f"🧭 **XS MOM** · {mode} · {d.get('gross', 0):g}배 · {head}"]
    eq, base, ml = d.get("equity"), d.get("start_equity"), d.get("max_loss")
    pnl = f" ({d['pnl_pct']:+.1%})" if d.get("pnl_pct") is not None else ""
    lines.append(f"잔고 {_usd(eq)} · 기준 {_usd(base)}{pnl}"
                 + (f" · 한도 {_usd(base * (1 - ml))} 아래면 청산(−{ml:.0%})" if base and ml else ""))
    if d.get("long_usd") is not None:
        lines.append(f"롱 {_usd(d.get('long_usd'))} · 숏 {_usd(d.get('short_usd'))} · 미실현 {d.get('upnl', 0):+.2f}"
                     f" · 포지션 {len(d.get('positions') or [])}개")
    lines.append(f"운영 {d.get('schedule', '—')} · 다음 리밸런스 {d.get('next_rebalance', '—')}")
    pos = sorted(d.get("positions") or [], key=lambda p: p.get("upnl") or 0)
    if pos:
        best = [p for p in pos[::-1][:3] if (p.get("upnl") or 0) > 0]
        worst = [p for p in pos[:3] if (p.get("upnl") or 0) < 0]
        f = lambda p: f"{p['symbol'].replace('USDT', '')} {p.get('side', '')} {p.get('upnl', 0):+.2f}"
        if best:
            lines.append("버는 중 " + " · ".join(map(f, best)))
        if worst:
            lines.append("잃는 중 " + " · ".join(map(f, worst)))
    if d.get("error"):
        lines.append(f"⚠️ {d['error']}")
    lines.append(f"(기준 {d.get('updated', '?')} · 5분마다 갱신)")
    return "\n".join(lines)[:1900]
