"""
Agent Harness Web UI - FastAPI + WebSocket

启动方式：
    cd /path/to/agent_harness
    python -m harness.web_ui.app

功能：
- 流式对话展示（WebSocket）
- SubAgent 执行过程实时展示
- Tool Call 可视化
- 多 Agent 切换（不丢失上下文）
- 多会话管理
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import logging
from pathlib import Path
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

os.chdir(_PROJECT_ROOT)

from harness.config import load_config
from harness.agents.registry import AgentRegistry
from harness.agents.agent_loop import build_agent, stream_agent, _load_prompt, _resolve_path
from harness.runtime import RuntimeContext, RunLifecycle
from harness.export.sft_recorder import record_run

from langchain_core.messages import HumanMessage

logger = logging.getLogger(__name__)

# ─── 全局配置 ─────────────────────────────────────────────────────────────────
_CONFIG_PATH = "harness.json"
_cfg = load_config(_CONFIG_PATH)
_registry = AgentRegistry(_cfg)

# 线程池
_executor = ThreadPoolExecutor(max_workers=4)

# 会话存储: session_id -> { agent_id, thread_id, messages, ctx, ... }
_sessions: dict[str, dict] = {}

# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(title="Agent Harness", docs_url=None, redoc_url=None)

# 静态文件
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ─── REST API ─────────────────────────────────────────────────────────────────

@app.get("/")
async def index():
    """返回主页面"""
    html_path = _STATIC_DIR / "index.html"
    return FileResponse(str(html_path), media_type="text/html")


@app.get("/api/agents")
async def list_agents():
    """列出所有可用的 agent"""
    agents = []
    for agent_cfg in _registry.list_all():
        agents.append({
            "id": agent_cfg.id,
            "name": agent_cfg.name,
            "type": agent_cfg.type,
            "description": agent_cfg.description or "",
            "default": agent_cfg.default,
            "subagents": agent_cfg.subagents,
        })
    return {"agents": agents}


@app.get("/api/sessions")
async def list_sessions():
    """列出所有会话"""
    sessions = []
    for sid, data in _sessions.items():
        sessions.append({
            "id": sid,
            "agent_id": data["agent_id"],
            "agent_name": data.get("agent_name", ""),
            "created_at": data.get("created_at", ""),
            "message_count": len(data.get("messages", [])),
            "title": data.get("title", "新对话"),
        })
    return {"sessions": sessions}


@app.post("/api/sessions")
async def create_session():
    """创建新会话"""
    from fastapi import Request
    session_id = uuid4().hex[:12]
    default_agent = _registry.get_default()
    agent_id = default_agent.id if default_agent else _registry.list_all()[0].id

    _sessions[session_id] = {
        "agent_id": agent_id,
        "agent_name": _registry.get(agent_id).name,
        "thread_id": f"session-{session_id}",
        "messages": [],
        "created_at": datetime.now().isoformat(),
        "title": "新对话",
    }
    return {"session_id": session_id, "agent_id": agent_id}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除会话"""
    if session_id in _sessions:
        del _sessions[session_id]
    return {"ok": True}


# ─── WebSocket 流式对话 ───────────────────────────────────────────────────────

@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()

    # 确保会话存在
    if session_id not in _sessions:
        default_agent = _registry.get_default()
        agent_id = default_agent.id if default_agent else _registry.list_all()[0].id
        _sessions[session_id] = {
            "agent_id": agent_id,
            "agent_name": _registry.get(agent_id).name,
            "thread_id": f"session-{session_id}",
            "messages": [],
            "created_at": datetime.now().isoformat(),
            "title": "新对话",
        }

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type", "")

            if msg_type == "chat":
                await _handle_chat(websocket, session_id, msg)
            elif msg_type == "switch_agent":
                await _handle_switch_agent(websocket, session_id, msg)
            elif msg_type == "get_history":
                await _handle_get_history(websocket, session_id)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("WebSocket error")
        try:
            await websocket.send_text(json.dumps({
                "type": "error",
                "content": f"连接错误: {str(e)[:200]}"
            }))
        except Exception:
            pass


async def _handle_get_history(websocket: WebSocket, session_id: str):
    """发送历史消息"""
    session = _sessions[session_id]
    await websocket.send_text(json.dumps({
        "type": "history",
        "messages": session["messages"],
        "agent_id": session["agent_id"],
        "agent_name": session.get("agent_name", ""),
    }))


async def _handle_switch_agent(websocket: WebSocket, session_id: str, msg: dict):
    """切换 agent（保留会话上下文）"""
    new_agent_id = msg.get("agent_id", "")
    if not new_agent_id:
        return

    try:
        agent_cfg = _registry.get(new_agent_id)
    except KeyError:
        await websocket.send_text(json.dumps({
            "type": "error",
            "content": f"Agent 不存在: {new_agent_id}"
        }))
        return

    session = _sessions[session_id]
    session["agent_id"] = new_agent_id
    session["agent_name"] = agent_cfg.name

    await websocket.send_text(json.dumps({
        "type": "agent_switched",
        "agent_id": new_agent_id,
        "agent_name": agent_cfg.name,
        "description": agent_cfg.description or "",
    }))


async def _handle_chat(websocket: WebSocket, session_id: str, msg: dict):
    """处理用户消息，流式返回 agent 回复"""
    content = msg.get("content", "").strip()
    if not content:
        return

    session = _sessions[session_id]
    agent_id = session["agent_id"]
    thread_id = session["thread_id"]

    # 记录用户消息
    user_msg = {
        "role": "user",
        "content": content,
        "timestamp": datetime.now().isoformat(),
    }
    session["messages"].append(user_msg)

    # 更新会话标题（取第一条消息的前20字）
    if len(session["messages"]) == 1:
        session["title"] = content[:20] + ("..." if len(content) > 20 else "")

    # 通知前端开始处理
    await websocket.send_text(json.dumps({"type": "start"}))

    agent_cfg = _registry.get(agent_id)

    # 设置环境变量
    os.environ["HARNESS_ROOT"] = _PROJECT_ROOT
    os.environ["HARNESS_AGENT_ID"] = agent_cfg.id

    ctx = RuntimeContext(agent_id=agent_id, harness_root=".", max_steps=agent_cfg.max_steps)

    os.environ["HARNESS_RUN_DIR"] = str(Path(ctx.run_dir).resolve())

    # 使用 asyncio.Queue 在线程和协程之间传递事件
    queue: asyncio.Queue = asyncio.Queue()

    def on_message(agent_name: str, msg_obj):
        """回调：agent 产生新消息"""
        tc = getattr(msg_obj, "tool_calls", None) or []
        c = getattr(msg_obj, "content", "") or ""

        if tc:
            for call in tc:
                args_data = call.get("args", {})
                queue.put_nowait({
                    "type": "tool_call",
                    "agent": agent_name,
                    "tool": call.get("name", "unknown"),
                    "args": args_data,
                    "call_id": call.get("id", ""),
                })

        if c:
            queue.put_nowait({
                "type": "assistant_message",
                "agent": agent_name,
                "content": c,
            })

    def on_event(event_type: str, data: dict):
        """回调：工具执行事件"""
        queue.put_nowait({
            "type": "tool_result",
            "event": event_type,
            **data,
        })

    def _run_agent():
        """在后台线程中运行 agent"""
        try:
            result = stream_agent(
                agent_cfg,
                harness_root=".",
                message=content,
                registry=_registry,
                run_dir=str(ctx.run_dir),
                thread_id=thread_id,
                on_message=on_message,
                on_subagent_event=on_event,
            )
            queue.put_nowait({"type": "_done", "result": result})
        except Exception as e:
            queue.put_nowait({"type": "_error", "error": str(e)})

    # 启动后台线程
    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(_executor, _run_agent)

    # 消费事件并发送给前端
    final_content = ""
    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=600)
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "content": "执行超时（10分钟）"
                }))
                break

            if event["type"] == "_done":
                # 提取最终回复
                result = event.get("result", {})
                messages = result.get("messages", [])
                for m in reversed(messages):
                    if getattr(m, "type", "") == "ai" and getattr(m, "content", ""):
                        final_content = m.content
                        break
                break

            elif event["type"] == "_error":
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "content": f"执行出错: {event['error'][:500]}"
                }))
                break

            elif event["type"] == "assistant_message":
                final_content = event["content"]
                await websocket.send_text(json.dumps({
                    "type": "assistant_message",
                    "agent": event.get("agent", ""),
                    "content": event["content"],
                }))

            elif event["type"] == "tool_call":
                # 序列化 args
                args = event.get("args", {})
                args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
                await websocket.send_text(json.dumps({
                    "type": "tool_call",
                    "agent": event.get("agent", ""),
                    "tool": event.get("tool", ""),
                    "args": args_str[:1000],
                    "call_id": event.get("call_id", ""),
                }))

            elif event["type"] == "tool_result":
                await websocket.send_text(json.dumps({
                    "type": "tool_result",
                    "agent": event.get("agent", ""),
                    "tool": event.get("tool", ""),
                    "output": event.get("output", "")[:2000],
                }))

    except WebSocketDisconnect:
        return

    await future

    # 记录 assistant 消息
    if final_content:
        session["messages"].append({
            "role": "assistant",
            "content": final_content,
            "timestamp": datetime.now().isoformat(),
        })

    # 发送完成信号
    await websocket.send_text(json.dumps({"type": "done"}))

    # 记录 SFT
    try:
        workspace_dir = str(_resolve_path(agent_cfg.workspace, _PROJECT_ROOT))
        system_prompt = _load_prompt(agent_cfg, workspace_dir, _PROJECT_ROOT)
        output_path = ctx.run_dir / "messages.jsonl"
        # record_run expects langchain messages, skip if we only have dict messages
    except Exception:
        pass


# ─── 启动入口 ─────────────────────────────────────────────────────────────────

def main():
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    print(f"\n  🚀 Agent Harness Web UI")
    print(f"  ➜ http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
