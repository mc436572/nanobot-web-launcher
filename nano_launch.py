"""
Gateway REST API（简约版）.
与 nanobot gateway 同进程时由 launcher 注入 agent/loop，未连接时返回 503.
gateway 启动参数由 nano_launch.json 指定（每条目对应 nanobot gateway 的 CLI 参数）.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# 启动后注入；未启动时为 None
_gateway_agent = None

app = FastAPI(title="Gateway REST", version="0.1.0")

# 项目根即 bot
_ROOT = Path(__file__).resolve().parent
UI_DIR = _ROOT / "ui"
# gateway 启动配置：与主程序同名的 JSON，每条目对应 nanobot gateway 的启动参数（-c/-p/-w/-v）
NANO_LAUNCH_JSON = _ROOT / "nano_launch.json"


def load_gateway_launch_config() -> dict[str, Any]:
    """读取 nano_launch.json，返回用于启动 nanobot gateway 的参数（config, bot_port, api_port, workspace, verbose）。"""
    if not NANO_LAUNCH_JSON.exists():
        return {"config": None, "bot_port": 18790, "api_port": 8000, "workspace": None, "verbose": False}
    with open(NANO_LAUNCH_JSON, encoding="utf-8") as f:
        return json.load(f)


def set_gateway(agent: Any) -> None:
    """注入 gateway 的 agent（单 loop 方案无需 loop 引用）。"""
    global _gateway_agent
    _gateway_agent = agent


def print_api_help(api_port: int) -> None:
    """启动时打印已封装的 FastAPI 接口，用作本机使用说明。"""
    print()
    print("=== Nano Launch API ===")
    print(f"Base URL: http://127.0.0.1:{api_port}/  (or http://localhost:{api_port}/)")
    print()
    print("Routes:")
    print()
    print("  POST  /chat")
    print('        Body: { "message": string, "session_key": string }')
    print("        说明: 向指定会话发送一条消息，并在同一请求中等待回复。")
    print('              - session_key 通常形如 "channel:chat_id"，例如:')
    print('                \"web:default\", \"telegram:123456789\"')
    print('              - 网页内置的 Web 会话使用 \"web:default\"。')
    print()
    print("   GET  /history?session_key=...")
    print("        说明: 返回【单个会话】的历史消息，而不是全部会话。")
    print("              - 必须提供 session_key 参数，规则同上。")
    print("              - 用于页面初始加载或刷新某条会话的对话记录。")
    print()
    print("   GET  /sessions")
    print("        说明: 列出当前 workspace 中的所有会话概览。")
    print("              - 返回字段示例:")
    print('                { "sessions": [')
    print('                    { "key": "web:default", "created_at": "...", "updated_at": "...", "path": "..." },')
    print('                    { "key": "telegram:123456789", ... }')
    print("                  ] }")
    print("              - 前端可用它来构建「渠道 / 会话」导航（按 key 前缀作为渠道名）。")
    print()


def _check_connected() -> None:
    if _gateway_agent is None:
        raise HTTPException(status_code=503, detail="Gateway not connected (launcher not running)")


class ChatIn(BaseModel):
    message: str
    session_key: str = "web:default"


class ChatOut(BaseModel):
    reply: str


@app.post("/chat", response_model=ChatOut)
async def chat(body: ChatIn) -> ChatOut:
    """发消息，同请求内等待 bot 回复。"""
    _check_connected()
    try:
        reply = await _gateway_agent.process_direct(
            body.message,
            session_key=body.session_key,
            channel="web",
            chat_id=body.session_key.split(":")[-1] if ":" in body.session_key else "default",
        )
    except TimeoutError:
        raise HTTPException(status_code=504, detail="Agent reply timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return ChatOut(reply=reply or "")


@app.get("/history")
async def history(session_key: str = "web:default") -> JSONResponse:
    """拉取某会话历史。"""
    _check_connected()
    try:
        messages = await _get_session_messages(session_key)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(content={"session_key": session_key, "messages": messages})


async def _get_session_messages(session_key: str) -> list[dict[str, Any]]:
    session = _gateway_agent.sessions.get_or_create(session_key)
    return list(session.messages)


@app.get("/sessions")
async def sessions() -> JSONResponse:
    """列出所有会话。"""
    _check_connected()
    try:
        items = await _list_sessions()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(content={"sessions": items})


async def _list_sessions() -> list[dict[str, Any]]:
    return _gateway_agent.sessions.list_sessions()


if UI_DIR.exists():
    app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
else:
    # 无 ui 目录时仍可启动，接口优先；未匹配的 GET 返回空白 404 页
    @app.get("/{full_path:path}")
    def _blank_404(full_path: str) -> HTMLResponse:
        return HTMLResponse(
            content="<!DOCTYPE html><html><head><meta charset='utf-8'></head><body></body></html>",
            status_code=404,
        )


async def _run_all_in_single_loop(cfg: dict[str, Any]) -> None:
    """
    单 loop 一键启动：
    - 按官方 gateway 的组装方式启动 agent/channels/cron/heartbeat（遵循原 config）
    - 同一 loop 内启动 FastAPI（附加能力）
    """
    import uvicorn
    from nanobot.cli.commands import _load_runtime_config, _make_provider, console, __logo__
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.channels.manager import ChannelManager
    from nanobot.config.paths import get_cron_dir
    from nanobot.cron.service import CronService
    from nanobot.cron.types import CronJob
    from nanobot.heartbeat.service import HeartbeatService
    from nanobot.session.manager import SessionManager
    from nanobot.utils.helpers import sync_workspace_templates

    config_path = cfg.get("config")
    if isinstance(config_path, str) and config_path:
        config_path = str((_ROOT / config_path).resolve()) if not Path(config_path).is_absolute() else config_path

    bot_port = cfg.get("bot_port", 18790)
    api_port = cfg.get("api_port", 8000)
    workspace_override = cfg.get("workspace")
    verbose = bool(cfg.get("verbose", False))

    if verbose:
        import logging
        logging.basicConfig(level=logging.DEBUG)

    config = _load_runtime_config(config_path, workspace_override)

    console.print(f"{__logo__} Starting nanobot gateway on port {bot_port}...")
    sync_workspace_templates(config.workspace_path)

    bus = MessageBus()
    provider = _make_provider(config)
    session_manager = SessionManager(config.workspace_path)

    # Cron service first (callback set after agent creation)
    cron_store_path = get_cron_dir() / "jobs.json"
    cron = CronService(cron_store_path)

    agent = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=config.workspace_path,
        model=config.agents.defaults.model,
        temperature=config.agents.defaults.temperature,
        max_tokens=config.agents.defaults.max_tokens,
        max_iterations=config.agents.defaults.max_tool_iterations,
        memory_window=config.agents.defaults.memory_window,
        reasoning_effort=config.agents.defaults.reasoning_effort,
        brave_api_key=config.tools.web.search.api_key or None,
        web_proxy=config.tools.web.proxy or None,
        exec_config=config.tools.exec,
        cron_service=cron,
        restrict_to_workspace=config.tools.restrict_to_workspace,
        session_manager=session_manager,
        mcp_servers=config.tools.mcp_servers,
        channels_config=config.channels,
    )
    set_gateway(agent)

    async def on_cron_job(job: CronJob) -> str | None:
        from nanobot.agent.tools.cron import CronTool
        from nanobot.agent.tools.message import MessageTool

        reminder_note = (
            "[Scheduled Task] Timer finished.\n\n"
            f"Task '{job.name}' has been triggered.\n"
            f"Scheduled instruction: {job.payload.message}"
        )

        cron_tool = agent.tools.get("cron")
        cron_token = None
        if isinstance(cron_tool, CronTool):
            cron_token = cron_tool.set_cron_context(True)
        try:
            response = await agent.process_direct(
                reminder_note,
                session_key=f"cron:{job.id}",
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to or "direct",
            )
        finally:
            if isinstance(cron_tool, CronTool) and cron_token is not None:
                cron_tool.reset_cron_context(cron_token)

        message_tool = agent.tools.get("message")
        if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
            return response

        if job.payload.deliver and job.payload.to and response:
            from nanobot.bus.events import OutboundMessage
            await bus.publish_outbound(OutboundMessage(
                channel=job.payload.channel or "cli",
                chat_id=job.payload.to,
                content=response,
            ))
        return response

    cron.on_job = on_cron_job

    channels = ChannelManager(config, bus)

    def _pick_heartbeat_target() -> tuple[str, str]:
        enabled = set(channels.enabled_channels)
        for item in session_manager.list_sessions():
            key = item.get("key") or ""
            if ":" not in key:
                continue
            channel, chat_id = key.split(":", 1)
            if channel in {"cli", "system"}:
                continue
            if channel in enabled and chat_id:
                return channel, chat_id
        return "cli", "direct"

    async def on_heartbeat_execute(tasks: str) -> str:
        channel, chat_id = _pick_heartbeat_target()

        async def _silent(*_args, **_kwargs):
            pass

        return await agent.process_direct(
            tasks,
            session_key="heartbeat",
            channel=channel,
            chat_id=chat_id,
            on_progress=_silent,
        )

    async def on_heartbeat_notify(response: str) -> None:
        from nanobot.bus.events import OutboundMessage
        channel, chat_id = _pick_heartbeat_target()
        if channel == "cli":
            return
        await bus.publish_outbound(OutboundMessage(channel=channel, chat_id=chat_id, content=response))

    hb_cfg = config.gateway.heartbeat
    heartbeat = HeartbeatService(
        workspace=config.workspace_path,
        provider=provider,
        model=agent.model,
        on_execute=on_heartbeat_execute,
        on_notify=on_heartbeat_notify,
        interval_s=hb_cfg.interval_s,
        enabled=hb_cfg.enabled,
    )

    if channels.enabled_channels:
        console.print(f"[green]✓[/green] Channels enabled: {', '.join(channels.enabled_channels)}")
    else:
        console.print("[yellow]Warning: No channels enabled[/yellow]")

    cron_status = cron.status()
    if cron_status["jobs"] > 0:
        console.print(f"[green]✓[/green] Cron: {cron_status['jobs']} scheduled jobs")

    console.print(f"[green]✓[/green] Heartbeat: every {hb_cfg.interval_s}s")

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=int(api_port),
            log_level="info",
            access_log=True,
            loop="asyncio",
        )
    )

    async def _serve_api():
        await server.serve()

    try:
        await cron.start()
        await heartbeat.start()
        await asyncio.gather(
            agent.run(),
            channels.start_all(),
            _serve_api(),
        )
    except KeyboardInterrupt:
        pass
    finally:
        try:
            await agent.close_mcp()
        except Exception:
            pass
        try:
            heartbeat.stop()
        except Exception:
            pass
        try:
            cron.stop()
        except Exception:
            pass
        try:
            agent.stop()
        except Exception:
            pass
        try:
            await channels.stop_all()
        except Exception:
            pass


if __name__ == "__main__":
    cfg = load_gateway_launch_config()
    try:
        print_api_help(int(cfg.get("api_port", 8000)))
    except Exception:
        pass
    asyncio.run(_run_all_in_single_loop(cfg))
