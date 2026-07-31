"""알림 전송 — Discord/Slack 로 한 줄 POST. stdlib 만(무거운 의존성 없음).

live.py 에 있던 notify 를 여기로 분리했다: 트레이더(engine.live)와 워치독(engine.watchdog)이
같은 알림 경로를 공유해야 하는데, live.py 는 numpy·TA-Lib·백테스트까지 끌어와서 워치독이 그걸
통째로 import 하면 안 된다. 이 모듈은 표준 라이브러리만 쓴다.

우선순위: DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID(봇 발신) > NOTIFY_WEBHOOK(웹훅). 둘 다 없으면 무시.
"""
from __future__ import annotations

import json
import os
import urllib.request

# 메시지 카테고리 → 전용 채널 환경변수. 미설정이면 기본 채널(DISCORD_CHANNEL_ID)로 폴백.
_CATEGORY_ENV = {"trade": "DISCORD_CHANNEL_TRADES",     # 진입/청산/포지션/멈춤·재개/가드레일
                 "system": "DISCORD_CHANNEL_SYSTEM",    # 기동/배포/워치독/에러/네트워크/전략·설정
                 "digest": "DISCORD_CHANNEL_DIGEST"}    # 정기 요약


# 알림 버튼(components). custom_id 는 discord_bot 의 persistent view 와 일치해야 클릭이 라우팅된다.
# 웹훅 모드에선 버튼이 무시된다(웹훅 메시지엔 인터랙티브 컴포넌트를 못 붙임).
CID_PAUSE, CID_RESUME, CID_FLATTEN, CID_STATUS = "act_pause", "act_resume", "act_flatten", "act_status"
_BUTTON_SPEC = {                                   # friendly key → 디스코드 버튼 스펙(style: 2=회색,3=초록,4=빨강,1=파랑)
    "pause":   {"cid": CID_PAUSE,   "label": "정지",     "emoji": "⏸", "style": 2},
    "resume":  {"cid": CID_RESUME,  "label": "재개",     "emoji": "▶️", "style": 3},
    "flatten": {"cid": CID_FLATTEN, "label": "즉시청산", "emoji": "🛑", "style": 4},
    "status":  {"cid": CID_STATUS,  "label": "상태",     "emoji": "📊", "style": 1},
}


def build_components(buttons) -> list:
    """friendly key 리스트(['pause','flatten']) → 디스코드 메시지 components(액션 로우). 없으면 []."""
    row = []
    for key in (buttons or [])[:5]:                # 한 로우 최대 5개
        s = _BUTTON_SPEC.get(key)
        if s:
            row.append({"type": 2, "style": s["style"], "label": s["label"],
                        "emoji": {"name": s["emoji"]}, "custom_id": s["cid"]})
    return [{"type": 1, "components": row}] if row else []


def channel_for(category: str = None) -> str:
    """카테고리 전용 채널 ID(있으면) → 없으면 기본 채널. 채널 라우팅 단일 지점.

    새 채널 변수를 안 넣으면 전부 기본 채널로 가서 동작이 하나도 안 바뀐다(하위호환).
    """
    if category:
        env = _CATEGORY_ENV.get(category)
        cid = os.environ.get(env) if env else None
        if cid:
            return cid
    return os.environ.get("DISCORD_CHANNEL_ID")


def notify(msg: str, category: str = None, buttons=None) -> None:
    """선택적 알림 — 봇 토큰이 있으면 봇으로, 없으면 웹훅으로 POST. 없으면 무시.

    category(trade/system/digest)에 따라 채널을 가른다(전용 채널 미설정 시 기본 채널 폴백).
    buttons(friendly key 리스트)는 봇 모드에서만 메시지에 붙는다 — 클릭은 discordbot 의 persistent
    view 가 처리(트레이더가 죽어도 discordbot 이 살아 있으면 동작). 웹훅 모드에선 버튼은 무시된다.
    봇 전송도 상주 Gateway 가 아니라 REST POST 한 방이라 여기(trader)에 discord.py 는 필요 없다.
    """
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel = channel_for(category)
    if token and channel:
        url = f"https://discord.com/api/v10/channels/{channel}/messages"
        payload, extra = {"content": msg}, {"Authorization": f"Bot {token}"}
        comps = build_components(buttons)
        if comps:
            payload["components"] = comps
    else:
        url = os.environ.get("NOTIFY_WEBHOOK")
        if not url:
            return
        if "hooks.slack.com" in url:
            payload = {"text": msg}
        elif "discord" in url:                   # discord.com / discordapp.com
            payload = {"content": msg}
        else:
            payload = {"content": msg, "text": msg}  # 알 수 없는 웹훅이면 둘 다(각자 모르는 키는 무시)
        extra = {}
    try:
        data = json.dumps(payload).encode()
        # User-Agent 필수: Discord 앞단 Cloudflare 가 파이썬 기본 UA(Python-urllib/x.y)를
        # 봇으로 보고 403(error code 1010)으로 막는다. 헤더 하나 빠졌다고 알림이 통째로
        # 안 왔고, 아래 except 가 조용히 삼켜서 안 오는 줄도 몰랐다.
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json", "User-Agent": "coin-coin-bot/1.0", **extra})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        # 알림 실패가 트레이딩을 막으면 안 되지만, 조용히 삼키면 몇 주를 모르고 지나간다.
        # 로그에는 남긴다(웹훅/봇이 죽어도 매매는 계속).
        print(f"  [알림 실패] {type(e).__name__}: {str(e)[:120]}", flush=True)
