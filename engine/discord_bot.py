"""디스코드 조회 봇 — 슬래시 명령(/status /position /stats)으로 봇 상태를 폰에서 본다.

대시보드와 같은 자리의 read-only '머리' 하나 더: data 볼륨의 state.json·trades.db 를 읽어
디스코드로 답한다(매매·수집엔 영향 0, 별개 프로세스). 단방향 웹훅(notify)과 달리 이건
**명령을 되받는다** → Gateway 봇(디스코드로 나가는 연결만, 공개 URL 불필요).

    python3 -m engine.discord_bot

필요 환경변수(.env):
    DISCORD_BOT_TOKEN        봇 토큰(필수)
    DISCORD_GUILD_ID         이 서버(길드)에만 명령 등록 → 즉시 반영(권장)
    DISCORD_ALLOWED_USER_IDS 콤마구분 유저ID 화이트리스트 — 이들만 응답(실돈 정보 보호)
    DISCORD_CHANNEL_ID       정기 요약을 보낼 채널(알림과 동일). 비면 요약 OFF.
    DIGEST_HOUR / DIGEST_TZ  정기 요약 시각(기본 8시 / Asia/Seoul — 컨테이너 TZ=UTC 라 명시).
    STATE_PATH / LEDGER_PATH / CONTROL_PATH 대시보드와 동일(data/state.json·trades.db·control.json)

명령: /status /position /stats /info (조회) · /control (시작/정지) · /strategy /config (변경).
보안: 모든 응답 ephemeral(요청자만) + 유저 화이트리스트(버튼·선택·모달 모두 재검문).
변경(/strategy·/config)은 확인 버튼을 한 번 더 거치고, 무포지션일 때만 적용된다(보유 중이면
청산 후 대기 — live.py 의 pendingStrategy/봇설정 반영과 같은 규칙). 강제청산·주문은 없다.
"""
from __future__ import annotations

import json
import os
import time

from . import control
from . import discord_views as views
from . import ledger
from . import preset


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
    channel_id = os.environ.get("DISCORD_CHANNEL_ID")     # 정기 요약 보낼 채널(알림과 동일)
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

    class ConfirmView(discord.ui.View):
        """[확인]/[취소] — 실돈 반영 전 한 번 더. 확인 시 apply_fn() 실행(예외는 메시지로)."""

        def __init__(self, apply_fn, applied_msg: str):
            super().__init__(timeout=120)
            self._apply_fn, self._applied_msg = apply_fn, applied_msg

        async def interaction_check(self, interaction) -> bool:
            if interaction.user.id in allowed:
                return True
            await interaction.response.send_message("⛔ 권한 없음", ephemeral=True)
            return False

        @discord.ui.button(label="확인", emoji="✅", style=discord.ButtonStyle.success)
        async def ok(self, interaction, button):
            try:
                self._apply_fn()
                msg = f"✅ {self._applied_msg}"
            except Exception as e:
                msg = f"⚠️ 실패: {e}"
            for c in self.children:
                c.disabled = True
            await interaction.response.edit_message(content=msg, view=self)

        @discord.ui.button(label="취소", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction, button):
            for c in self.children:
                c.disabled = True
            await interaction.response.edit_message(content="취소됨.", view=self)

    class StrategySelect(discord.ui.Select):
        """전략 선택 드롭다운 → 고르면 확인 단계로. Discord 옵션 상한 25개."""

        def __init__(self, strategies):
            self._strategies = strategies[:25]
            opts = [discord.SelectOption(label=(s["name"] or "?")[:100], value=str(i),
                                         description=f"{s['symbol']} {s['timeframe']}"[:100])
                    for i, s in enumerate(self._strategies)]
            super().__init__(placeholder="전략 선택…", options=opts, min_values=1, max_values=1)

        async def callback(self, interaction):
            choice = self._strategies[int(self.values[0])]
            has_pos = bool(load_state(state_path).get("position"))
            view = ConfirmView(lambda: preset.select_strategy(choice["path"]),
                               f"전략 전환 예약: {choice['name']}")
            await interaction.response.edit_message(
                content=views.strategy_confirm_text(choice, has_pos), view=view)

    class StrategyView(discord.ui.View):
        def __init__(self, strategies):
            super().__init__(timeout=120)
            self.add_item(StrategySelect(strategies))

        async def interaction_check(self, interaction) -> bool:
            if interaction.user.id in allowed:
                return True
            await interaction.response.send_message("⛔ 권한 없음", ephemeral=True)
            return False

    class ConfigModal(discord.ui.Modal, title="봇 설정 수정"):
        """레버리지·자본비중·maker 타임아웃·심볼 편집. 빈 칸은 유지. 제출 → 확인 단계."""

        def __init__(self, eff: dict):
            super().__init__()
            self._symbol = discord.ui.TextInput(
                label="심볼(빈칸=유지)", required=False, default=str(eff.get("symbol") or ""))
            self._leverage = discord.ui.TextInput(
                label="레버리지 1~125", required=False, default=str(eff.get("leverage") or ""))
            self._equity = discord.ui.TextInput(
                label="자본비중 %(0~100)", required=False,
                default=str(eff.get("equityPercent") if eff.get("equityPercent") is not None else ""))
            self._timeout = discord.ui.TextInput(
                label="maker 타임아웃(초)", required=False,
                default=str(eff.get("makerTimeoutSeconds") if eff.get("makerTimeoutSeconds") is not None else ""))
            for it in (self._symbol, self._leverage, self._equity, self._timeout):
                self.add_item(it)

        async def on_submit(self, interaction):
            edits, errors = views.parse_config_form({
                "symbol": self._symbol.value, "leverage": self._leverage.value,
                "equity_percent": self._equity.value, "maker_timeout": self._timeout.value})
            if errors:
                await interaction.response.send_message("⚠️ " + " / ".join(errors), ephemeral=True)
                return
            if not edits:
                await interaction.response.send_message("변경할 값이 없습니다.", ephemeral=True)
                return
            has_pos = bool(load_state(state_path).get("position"))

            def apply():
                new = views.apply_config_edits(control.get_bot_config(control_path), edits)
                control.set_bot_config(new, path=control_path)

            await interaction.response.send_message(
                views.config_confirm_text(edits, has_pos),
                view=ConfirmView(apply, "봇 설정 반영됨"), ephemeral=True)

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

    @tree.command(name="info", description="지금 돌고 있는 봇의 설정(전략·레버리지·사이징·실행)")
    async def info_cmd(interaction: "discord.Interaction"):
        if not await guard(interaction):
            return
        await interaction.response.send_message(
            views.info_text(load_state(state_path), preset.bot_config_info()), ephemeral=True)

    @tree.command(name="strategy", description="전략 전환 — 프리셋 목록에서 선택(무포지션 시 적용)")
    async def strategy_cmd(interaction: "discord.Interaction"):
        if not await guard(interaction):
            return
        strategies = preset.list_strategies()
        if not strategies:
            await interaction.response.send_message("사용 가능한 전략이 없습니다.", ephemeral=True)
            return
        cur = (load_state(state_path) or {}).get("preset", "?")
        await interaction.response.send_message(
            f"현재 전략: **{cur}**\n바꿀 전략을 고르세요:", view=StrategyView(strategies), ephemeral=True)

    @tree.command(name="config", description="봇 설정 수정 — 레버리지·자본비중·maker·심볼(무포지션 시 적용)")
    async def config_cmd(interaction: "discord.Interaction"):
        if not await guard(interaction):
            return
        eff = views.effective_config(preset.bot_config_info())
        await interaction.response.send_modal(ConfigModal(eff))

    # ── 정기 요약: 매일 지정 시각에 지난 24시간 거래를 채널로 push ──
    import datetime as _dt
    from discord.ext import tasks
    try:
        from zoneinfo import ZoneInfo
        digest_tz = ZoneInfo(os.environ.get("DIGEST_TZ", "Asia/Seoul"))   # 컨테이너 TZ=UTC 라 명시
    except Exception:
        digest_tz = _dt.timezone.utc
    try:
        digest_hour = int(os.environ.get("DIGEST_HOUR", "8"))
    except ValueError:
        digest_hour = 8

    # 요약은 전용 채널(DISCORD_CHANNEL_DIGEST)로, 없으면 기본 채널로 폴백.
    digest_channel = os.environ.get("DISCORD_CHANNEL_DIGEST") or channel_id

    @tasks.loop(time=_dt.time(hour=digest_hour, minute=0, tzinfo=digest_tz))
    async def daily_digest():
        if not digest_channel:
            return
        try:
            ch = client.get_channel(int(digest_channel)) or await client.fetch_channel(int(digest_channel))
            start = int(time.time() * 1000) - 24 * 3600 * 1000
            rows = ledger.load(ledger_path, start_ms=start)
            st = ledger.stats(ledger_path, start_ms=start)
            await ch.send(views.daily_digest_text(rows, st, load_state(state_path)))
        except Exception as e:
            print(f"[디스코드봇] 일일 요약 전송 실패: {e}", flush=True)

    @client.event
    async def on_ready():
        try:
            if guild_id:
                g = discord.Object(id=int(guild_id))
                tree.copy_global_to(guild=g)          # 길드에 복사 → 즉시 반영(글로벌은 최대 1시간)
                await tree.sync(guild=g)
            else:
                await tree.sync()
            if digest_channel and not daily_digest.is_running():
                daily_digest.start()                  # 매일 digest_hour 시(digest_tz)에 요약 push
            print(f"[디스코드봇] 로그인 {client.user} · 명령 동기화 완료 · "
                  f"일일요약 {digest_hour}시({digest_tz}) {'ON' if digest_channel else 'OFF(채널 미설정)'}", flush=True)
        except Exception as e:
            print(f"[디스코드봇] 명령 동기화 실패: {e}", flush=True)

    client.run(token)


def main() -> None:
    from .env import load_dotenv
    load_dotenv()
    # 토큰이 없으면 크래시 루프(restart:unless-stopped) 대신 조용히 유휴 대기 —
    # 배포 폴러가 이 서비스를 띄워도, 아직 설정 전이면 로그를 어지럽히지 않는다.
    if not os.environ.get("DISCORD_BOT_TOKEN"):
        print("[디스코드봇] DISCORD_BOT_TOKEN 미설정 — 유휴 대기(토큰 설정 후 재시작하면 활성).", flush=True)
        while True:
            time.sleep(3600)
    run()


if __name__ == "__main__":
    main()
