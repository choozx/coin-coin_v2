"""디스코드 조회 봇 — 슬래시 명령(/status /position /stats)으로 봇 상태를 폰에서 본다.

대시보드와 같은 자리의 read-only '머리' 하나 더: data 볼륨의 state.json·trades.db 를 읽어
디스코드로 답한다(매매·수집엔 영향 0, 별개 프로세스). 단방향 웹훅(notify)과 달리 이건
**명령을 되받는다** → Gateway 봇(디스코드로 나가는 연결만, 공개 URL 불필요).

    python3 -m engine.discord_bot

필요 환경변수(.env):
    DISCORD_BOT_TOKEN        봇 토큰(필수)
    DISCORD_GUILD_ID         이 서버(길드)에만 명령 등록 → 즉시 반영(권장)
    DISCORD_ALLOWED_USER_IDS 콤마구분 유저ID 화이트리스트 — 이들만 응답(실돈 정보 보호)
    STATE_PATH / LEDGER_PATH / CONTROL_PATH 대시보드와 동일(data/state.json·trades.db·control.json)

명령: /status /position /stats (조회) · /control (봇 시작/정지 버튼).
보안: 모든 응답은 ephemeral(요청자만 봄) + 유저 화이트리스트. /control 버튼도 클릭 시 재검문.
제어는 '우아한 정지'(control.paused)까지만 — 강제청산·주문 같은 파괴적 행위는 없다.
"""
from __future__ import annotations

import json
import os
import time

from . import control
from . import discord_views as views
from . import ledger


def load_state(path: str) -> dict:
    """state.json 읽기 — 없거나 깨져도 예외 대신 {'error':...} 로(대시보드와 같은 관용)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"error": "상태 파일 없음 — 봇이 아직 안 돌았거나 state.json 미생성"}
    except Exception as e:
        return {"error": str(e)}


def allowed_ids(raw: str = None) -> set:
    """DISCORD_ALLOWED_USER_IDS 파싱 → 정수 집합. 비면 빈 집합(=아래서 '전원 거부'로 처리)."""
    raw = os.environ.get("DISCORD_ALLOWED_USER_IDS", "") if raw is None else raw
    return {int(x) for x in raw.replace(" ", "").split(",") if x.strip().isdigit()}


def run() -> None:
    try:
        import discord
        from discord import app_commands
    except ImportError:
        raise RuntimeError("discord.py 미설치 — pip install 'discord.py>=2.3' (디스코드 봇 전용).")

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN 없음(.env) — 디스코드 개발자 포털에서 봇 토큰 발급.")
    state_path = os.environ.get("STATE_PATH", "data/state.json")
    ledger_path = os.environ.get("LEDGER_PATH", ledger.LEDGER_PATH)
    control_path = os.environ.get("CONTROL_PATH", control.DEFAULT_PATH)
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    allowed = allowed_ids()
    if not allowed:
        # 화이트리스트가 비면 아무도 못 쓴다 — 실돈 정보를 실수로 전체공개하지 않기 위한 안전기본값.
        print("[디스코드봇] ⚠️ DISCORD_ALLOWED_USER_IDS 미설정 — 모든 요청을 거부합니다.", flush=True)

    intents = discord.Intents.none()          # 슬래시 명령엔 특권 인텐트 불필요
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    async def guard(interaction) -> bool:
        """화이트리스트 밖이면 ephemeral 로 거부하고 False. 통과하면 True."""
        if interaction.user.id in allowed:
            return True
        await interaction.response.send_message(
            "⛔ 권한 없음 — 허용된 사용자만 조회할 수 있습니다.", ephemeral=True)
        return False

    def control_panel() -> str:
        """control.json(현재 정지 여부) + state.json(포지션 보유) → 패널 텍스트."""
        paused = control.service_state("trader", control_path) == "paused"
        st = load_state(state_path)
        return views.control_text(paused, bool(st.get("position")))

    class ControlView(discord.ui.View):
        """봇 시작/정지 버튼. ephemeral 메시지에 붙어 요청자만 보고 누른다(+클릭 시 재검문)."""

        def __init__(self):
            super().__init__(timeout=300)          # 5분 후 버튼 비활성(만료된 패널 오작동 방지)

        async def interaction_check(self, interaction) -> bool:
            if interaction.user.id in allowed:
                return True
            await interaction.response.send_message("⛔ 권한 없음", ephemeral=True)
            return False

        async def _set(self, interaction, state: str):
            control.set_service("trader", state, path=control_path)
            # 버튼을 그대로 둔 채 패널 텍스트만 새 상태로 갱신(연속 조작 가능).
            await interaction.response.edit_message(content=control_panel(), view=self)

        @discord.ui.button(label="시작", emoji="▶️", style=discord.ButtonStyle.success)
        async def start(self, interaction, button):
            await self._set(interaction, "running")

        @discord.ui.button(label="정지", emoji="⏸", style=discord.ButtonStyle.danger)
        async def stop(self, interaction, button):
            await self._set(interaction, "paused")

    @tree.command(name="status", description="봇 상태·포지션·잔고 요약")
    async def status(interaction: "discord.Interaction"):
        if not await guard(interaction):
            return
        await interaction.response.send_message(views.status_text(load_state(state_path)), ephemeral=True)

    @tree.command(name="position", description="현재 보유 포지션 상세(현재가·미실현손익)")
    async def position(interaction: "discord.Interaction"):
        if not await guard(interaction):
            return
        await interaction.response.send_message(views.position_text(load_state(state_path)), ephemeral=True)

    @tree.command(name="stats", description="매매 성과(승률·손익비·MDD)")
    @app_commands.describe(period="집계 기간")
    @app_commands.choices(period=[
        app_commands.Choice(name="오늘", value="today"),
        app_commands.Choice(name="7일", value="7d"),
        app_commands.Choice(name="30일", value="30d"),
        app_commands.Choice(name="전체", value="all"),
    ])
    async def stats(interaction: "discord.Interaction", period: str = "all"):
        if not await guard(interaction):
            return
        start, label = views.period_bounds(period, int(time.time() * 1000))
        s = ledger.stats(ledger_path, start_ms=start)
        await interaction.response.send_message(views.stats_text(s, label), ephemeral=True)

    @tree.command(name="control", description="봇 시작/정지 (graceful — 보유 포지션은 계속 관리)")
    async def control_cmd(interaction: "discord.Interaction"):
        if not await guard(interaction):
            return
        await interaction.response.send_message(control_panel(), view=ControlView(), ephemeral=True)

    @client.event
    async def on_ready():
        try:
            if guild_id:
                g = discord.Object(id=int(guild_id))
                tree.copy_global_to(guild=g)          # 길드에 복사 → 즉시 반영(글로벌은 최대 1시간)
                await tree.sync(guild=g)
            else:
                await tree.sync()
            print(f"[디스코드봇] 로그인 {client.user} · 명령 동기화 완료 "
                  f"(허용 유저 {len(allowed)}명)", flush=True)
        except Exception as e:
            print(f"[디스코드봇] 명령 동기화 실패: {e}", flush=True)

    client.run(token)


def main() -> None:
    from .env import load_dotenv
    load_dotenv()
    run()


if __name__ == "__main__":
    main()
