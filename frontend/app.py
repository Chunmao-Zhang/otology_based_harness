#!/usr/bin/env python3
"""KnowCode KC-Agent frontend.

Run from the repository root:
    PYTHONPATH=. python3 workspaces/deepagents_kbqa_general/frontend/app.py
"""

from __future__ import annotations

import asyncio
import ast
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from harness.agents.agent_loop import _load_prompt, _resolve_path, build_agent
from harness.agents.registry import AgentRegistry
from harness.config import load_config
from harness.export.sft_recorder import record_run
from harness.runtime import RunLifecycle, RuntimeContext
from workspaces.deepagents_kbqa_general.code.graph_runtime import (
    activate_graph_scope,
    delete_graph,
    finalize_graph_import,
    get_graph,
    import_uploaded_graph,
    list_graphs,
    list_graphs_light,
    read_active_scope,
    register_uploaded_graph,
    resolve_scope_env,
    summarize_graph,
)


AGENT_ID = "deepagents_kbqa_general"
UI_BRAND = "Knowledge Computing Agent (KC-Agent)"
THREAD_PREFIX = "kbqa-ui"
STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_DIR = ROOT / "outputs" / "deepagents_kbqa_general" / "frontend_sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

# The API key must come from the environment (e.g. a local .env). Never commit a
# plaintext key — see .env.example for the variables this app expects.
if not os.environ.get("SILICONFLOW_API_KEY"):
    os.environ.setdefault("SILICONFLOW_API_KEY", "")

CONFIG_PATH = Path(os.environ.get("HARNESS_CONFIG", ROOT / "harness.json")).expanduser()
CONFIG = load_config(CONFIG_PATH)
REGISTRY = AgentRegistry(CONFIG)
AGENT_CFG = REGISTRY.get(AGENT_ID)
EXECUTOR = ThreadPoolExecutor(max_workers=8)
# Registry lock: serialises quick ledger/registry writes (register, delete).
_REGISTRY_LOCK = threading.Lock()
# Finalize lock: serialises expensive runtime builds so they don't conflict on shared processed dirs.
_FINALIZE_LOCK = threading.Lock()
# Tiny in-process cache for /api/health (avoids re-reading the multi-MB active_graph.json per request).
HEALTH_CACHE: dict[str, Any] = {}
FRONTEND_SCRIPT = Path(__file__).resolve()
RUN_IDLE_TIMEOUT_SECONDS = int(os.environ.get("KBQA_AGENT_IDLE_TIMEOUT_SECONDS", "180"))
RUN_HARD_TIMEOUT_SECONDS = int(os.environ.get("KBQA_AGENT_HARD_TIMEOUT_SECONDS", "900"))
SESSION_EVENT_LIMIT = int(os.environ.get("KBQA_SESSION_EVENT_LIMIT", "120"))
EMPTY_FINAL_CONTINUATIONS = int(os.environ.get("KBQA_EMPTY_FINAL_CONTINUATIONS", "2"))


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def session_path(session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid session id")
    return SESSION_DIR / f"{safe}.json"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def prune_session_events(session: dict[str, Any], limit: int = SESSION_EVENT_LIMIT) -> None:
    """Keep chat files small while preserving user/assistant turns."""

    if limit <= 0:
        return
    messages = list(session.get("messages", []) or [])
    event_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "event"
    ]
    if len(event_indexes) <= limit:
        return
    keep = set(event_indexes[-limit:])
    session["messages"] = [
        message
        for index, message in enumerate(messages)
        if not (isinstance(message, dict) and message.get("role") == "event") or index in keep
    ]


def read_general_stats() -> dict[str, Any]:
    manifest = ROOT / "data" / "deepagents_kbqa_general" / "runtime" / "general_env" / "active_graph.json"
    if not manifest.exists():
        return {}
    try:
        data = read_json(manifest)
    except Exception:
        return {}
    # NOTE: do not echo back `graph_catalog` here — the active_graph.json file
    # can be several MB and we only need scalar stats for /api/health.
    return {
        "entity_count": data.get("entity_count"),
        "relation_count": data.get("relation_count"),
        "triple_count": data.get("triple_count"),
        "graph_count": data.get("graph_count"),
        "entity_type_count": len(data.get("entity_type_counts", {}) or {}),
        "format_contract": data.get("format_contract", ""),
        "generated_at": data.get("generated_at"),
    }


def read_general_relation_types() -> dict[str, str]:
    manifest = ROOT / "data" / "deepagents_kbqa_general" / "runtime" / "general_env" / "active_graph.json"
    if not manifest.exists():
        return {}
    try:
        data = read_json(manifest)
    except Exception:
        return {}
    relation_types = data.get("relation_types", {})
    return relation_types if isinstance(relation_types, dict) else {}


def read_general_relation_schemas() -> dict[str, dict[str, str]]:
    manifest = ROOT / "data" / "deepagents_kbqa_general" / "runtime" / "general_env" / "active_graph.json"
    if not manifest.exists():
        return {}
    try:
        data = read_json(manifest)
    except Exception:
        return {}
    relation_schemas = data.get("relation_schemas", {})
    return relation_schemas if isinstance(relation_schemas, dict) else {}

def project_metadata() -> dict[str, Any]:
    skills_dir = ROOT / "workspaces" / "deepagents_kbqa_general" / "skills"
    skills = sorted(path.name for path in skills_dir.iterdir() if path.is_dir()) if skills_dir.exists() else []
    relations = read_general_relation_types()
    relation_schemas = read_general_relation_schemas()
    relation_cards = [
        {
            "source": relation_schemas.get(rel, {}).get("head_type", "Entity"),
            "predicate": relation_schemas.get(rel, {}).get("name", rel),
            "target": relation_schemas.get(rel, {}).get("tail_type", "Entity"),
            "type": rel_type,
        }
        for rel, rel_type in list(relations.items())[:24]
    ]
    return {
        "workspace": str(ROOT / "workspaces" / "deepagents_kbqa_general"),
        "tools": [
            "search_entity",
            "search_predicate",
            "list_predicates_by_entity",
            "search_entity_by_predicate",
            "build_subgraph_schema",
            "execute_code",
        ],
        "skills": skills,
        "dataset": read_general_stats(),
        "graph_schema": {
            "title": "Generic File-Backed Knowledge Graph",
            "subtitle": "Uploaded TXT, JSON, or XLSX typed triples are prepared into SQLite + ChromaDB for KBQA.",
            "nodes": [
                {"id": "entity", "label": "Typed Entity", "description": "Each node is keyed by type + name, or by an optional uploaded id."},
                {"id": "predicate", "label": "Typed Relation", "description": "Relations are schemas of HeadType - relation -> TailType."},
                {"id": "subgraph", "label": "Subgraph", "description": "Tool-selected local evidence serialized as a schema file."},
                {"id": "answer", "label": "Boxed Answer", "description": "Final normalized names emitted as \\boxed{[...]}."},
            ],
            "relations": relation_cards,
            "pipeline": [
                {"label": "Entity Link", "detail": "search_entity maps text mentions to exact entity names in the active graph."},
                {"label": "Predicate Probe", "detail": "search_predicate and list_predicates_by_entity identify candidate relations."},
                {"label": "Subgraph Schema", "detail": "build_subgraph_schema materializes a class-like graph view."},
                {"label": "Code Reasoning", "detail": "execute_code filters paths and formats boxed answers."},
            ],
        },
        "runtime": str(ROOT / "data" / "deepagents_kbqa_general" / "runtime" / "general_env"),
        "smoke_tests": [
            "workspaces/deepagents_kbqa_general/code/prepare.py",
            "workspaces/deepagents_kbqa_general/code/e2e_smoke.py",
            "workspaces/deepagents_kbqa_general/code/metaqa_graph_import_smoke.py",
            "workspaces/deepagents_kbqa_general/code/metaqa_entity_search_smoke.py",
        ],
    }


class SessionStore:
    def create(self) -> dict[str, Any]:
        session_id = uuid4().hex[:12]
        session = {
            "id": session_id,
            "title": "New conversation",
            "agent_id": AGENT_ID,
            "agent_name": UI_BRAND,
            "thread_id": f"{THREAD_PREFIX}-{session_id}",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "messages": [],
        }
        self.save(session)
        return session

    def list(self) -> list[dict[str, Any]]:
        sessions = []
        for path in SESSION_DIR.glob("*.json"):
            try:
                item = read_json(path)
            except Exception:
                continue
            sessions.append(
                {
                    "id": item["id"],
                    "title": item.get("title") or "New conversation",
                    "agent_id": item.get("agent_id", AGENT_ID),
                    "agent_name": item.get("agent_name", UI_BRAND),
                    "created_at": item.get("created_at", ""),
                    "updated_at": item.get("updated_at", ""),
                    "message_count": len(item.get("messages", [])),
                }
            )
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)

    def get(self, session_id: str) -> dict[str, Any]:
        path = session_path(session_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Session not found")
        return read_json(path)

    def save(self, session: dict[str, Any]) -> None:
        prune_session_events(session)
        session["updated_at"] = now_iso()
        write_json(session_path(session["id"]), session)

    def delete(self, session_id: str) -> None:
        path = session_path(session_id)
        if path.exists():
            path.unlink()


STORE = SessionStore()
app = FastAPI(title=UI_BRAND, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def no_cache_frontend_assets(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/")
async def index():
    return FileResponse(
        STATIC_DIR / "index.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/health")
async def health():
    # Cache the heavy stats for 1 s so concurrent page loads don't all re-read
    # the multi-megabyte active_graph.json manifest.
    now = time.time()
    cached = HEALTH_CACHE.get("data")
    if cached and (now - cached["at"]) < 1.0:
        data = cached["payload"]
    else:
        data = {
            "ok": True,
            "agent_id": AGENT_ID,
            "agent_name": UI_BRAND,
            "model": AGENT_CFG.model.model_id,
            "max_steps": AGENT_CFG.max_steps,
            "api_key_present": bool((AGENT_CFG.model and AGENT_CFG.model.api_key) or os.environ.get("SILICONFLOW_API_KEY")),
            "sessions_dir": str(SESSION_DIR),
            "project": project_metadata(),
            "streaming": {
                "mode": "frontend-token-stream",
                "harness_modified": False,
                "description": "The frontend app streams LangGraph message chunks over WebSocket without changing harness code.",
            },
        }
        if AGENT_ID == "deepagents_kbqa_general":
            data["graph_runtime"] = read_general_stats()
            data["graph_scope"] = read_active_scope()
        HEALTH_CACHE["data"] = {"at": now, "payload": data}
    return data


@app.get("/api/graphs/imports")
async def list_graph_imports(light: int = 0):
    if light:
        return {"imports": list_graphs_light(limit=100)}
    return {"imports": list_graphs(limit=100)}


@app.get("/api/graphs/summary")
async def graph_summary(graph_id: str = ""):
    try:
        return summarize_graph(graph_id or None)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/graphs/imports")
async def import_graph(
    file: UploadFile = File(...),
    dataset_name: str = Form(default=""),
):
    raw_content = await file.read()

    def register_thread() -> dict[str, Any]:
        with _REGISTRY_LOCK:
            return register_uploaded_graph(
                file.filename or "uploaded_graph.txt",
                raw_content,
                dataset_name,
            )

    try:
        report = await asyncio.to_thread(register_thread)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Kick off expensive runtime build in background — do not block the response.
    graph_id = report.get("id", "")

    def background_finalize() -> None:
        try:
            with _FINALIZE_LOCK:
                finalize_graph_import(graph_id)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Background finalize failed for graph %s", graph_id)

    EXECUTOR.submit(background_finalize)

    return {"ok": True, "import": report}


@app.get("/api/graphs/imports/{graph_id}/status")
async def graph_import_status(graph_id: str):
    graph = get_graph(graph_id)
    if not graph:
        raise HTTPException(status_code=404, detail="Graph not found")
    return {
        "id": graph_id,
        "status": graph.get("status", "ready"),
        "stats": graph.get("stats", {}),
        "progress": graph.get("progress"),
    }


@app.post("/api/graphs/imports/{graph_id}/warm")
async def warm_graph_scope(graph_id: str):
    """Kick off a background build of the scope runtime for the given graph.

    The frontend fires this whenever the user selects (or auto-picks) a
    knowledge base, so the first chat question on that scope doesn't pay the
    cost of building the vector index / SQLite / ChromaDB from scratch. No-op
    if the scope is already built.
    """
    if not get_graph(graph_id):
        raise HTTPException(status_code=404, detail="Graph not found")

    def warm_thread() -> None:
        try:
            resolve_scope_env(graph_id)
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Background warm failed for graph %s", graph_id
            )

    EXECUTOR.submit(warm_thread)
    return {"ok": True, "status": "warming", "graph_id": graph_id}


@app.get("/api/graphs/imports/{graph_id}/warm/status")
async def warm_graph_scope_status(graph_id: str):
    """Return the current warm progress for the given graph scope.

    The frontend polls this every ~500ms while the progress bar is visible.
    The state machine is:
      - "ready"     : active_graph.json exists on disk — chat can proceed.
      - "warming"   : a build is in flight, current progress is reported.
      - "queued"    : another caller holds the per-scope build lock; the
                      poll will pick up real progress as soon as the lead
                      caller publishes it.
      - "error"     : the build failed; the message field carries the error.
      - "idle"      : nothing has been requested yet.
    """
    graph = get_graph(graph_id)
    if not graph:
        raise HTTPException(status_code=404, detail="Graph not found")

    # Did the build actually finish (active_graph.json written by
    # write_processed_artifacts with activate=True)?
    from workspaces.deepagents_kbqa_general.code.graph_runtime import (
        build_scope_runtime,  # noqa: F401  (imported to ensure path init)
        read_scope_warm_progress,
        slugify,
        SCOPE_ENV_PREFIX,
    )
    from workspaces.deepagents_kbqa_general.code.prepare import DEFAULT_RUNTIME_ROOT

    safe_id = slugify(graph_id, "graph")
    env_dir = DEFAULT_RUNTIME_ROOT / SCOPE_ENV_PREFIX / safe_id
    active_marker = env_dir / "active_graph.json"
    if active_marker.exists():
        return {
            "state": "ready",
            "graph_id": graph_id,
            "progress": 1.0,
            "stage": "ready",
            "message": "Knowledge base is ready.",
            "show_progress": True,
        }

    progress = read_scope_warm_progress(graph_id)
    if progress is not None:
        return {"state": "warming", "graph_id": graph_id, **progress}

    # No active warm in flight — distinguish "queued behind another caller"
    # from "never started". The per-scope build lock makes the second case
    # rare; report it as "queued" so the UI keeps the bar visible.
    return {
        "state": "queued",
        "graph_id": graph_id,
        "progress": 0.0,
        "stage": "queued",
        "message": "Waiting for the previous build to finish…",
        "show_progress": True,
    }


@app.delete("/api/graphs/imports/{graph_id}")
async def delete_graph_import(graph_id: str):
    def delete_graph_thread() -> dict[str, Any]:
        with _REGISTRY_LOCK:
            return delete_graph(graph_id)

    try:
        report = await asyncio.to_thread(delete_graph_thread)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, **report}


@app.get("/api/sessions")
async def list_sessions():
    return {"sessions": STORE.list()}


@app.post("/api/sessions")
async def create_session():
    return {"session": STORE.create()}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    return {"session": STORE.get(session_id)}


@app.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, payload: dict[str, Any]):
    session = STORE.get(session_id)
    title = str(payload.get("title", "")).strip()
    if title:
        session["title"] = title[:80]
        STORE.save(session)
    return {"session": session}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    STORE.delete(session_id)
    return {"ok": True}


def ui_message(role: str, content: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "id": uuid4().hex[:10],
        "role": role,
        "content": content,
        "timestamp": now_iso(),
        **extra,
    }


def compact(value: Any, limit: int = 4000) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    return text if len(text) <= limit else text[:limit] + "\n... [truncated]"


def content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(value)


def message_type(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("type") or message.get("role") or "")
    return str(getattr(message, "type", "") or "")


def message_name(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("name") or "")
    return str(getattr(message, "name", "") or "")


def message_content(message: Any) -> str:
    if isinstance(message, dict):
        return content_to_text(message.get("content", ""))
    return content_to_text(getattr(message, "content", ""))


def message_tool_calls(message: Any) -> list[Any]:
    if isinstance(message, dict):
        calls = message.get("tool_calls") or []
    else:
        calls = getattr(message, "tool_calls", None) or []
    return list(calls) if isinstance(calls, (list, tuple)) else []


def reasoning_from_chunk(chunk: Any) -> str:
    """Extract incremental DeepSeek thinking-mode text from a streamed chunk.

    DeepSeekChatOpenAI stores reasoning-mode output on
    ``additional_kwargs['reasoning_content']`` for each chunk.
    """

    kwargs = getattr(chunk, "additional_kwargs", None)
    if isinstance(kwargs, dict):
        reasoning = kwargs.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            return reasoning
    return ""


def read_marked_exec_script(path_text: str) -> str | None:
    """Read a hidden execute_code script marker without allowing arbitrary paths."""

    raw = str(path_text or "").strip()
    if not raw:
        return None

    marker_path = Path(raw).expanduser()
    runtime_root = Path(os.environ.get("KBQA_RUNTIME_ROOT") or ROOT / "data" / "deepagents_kbqa_general" / "runtime")
    candidates = [marker_path] if marker_path.is_absolute() else [ROOT / marker_path, runtime_root / marker_path]

    allowed_roots = [ROOT.resolve()]
    try:
        allowed_roots.append(runtime_root.resolve())
    except Exception:
        pass

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if not any(resolved == root or root in resolved.parents for root in allowed_roots):
            continue
        if resolved.is_file():
            return resolved.read_text(encoding="utf-8")
    return None


def last_message(result_state: dict[str, Any]) -> Any | None:
    messages = result_state.get("messages", []) if isinstance(result_state, dict) else []
    if not isinstance(messages, list) or not messages:
        return None
    for msg in reversed(messages):
        if (
            message_type(msg) == "ai"
            and not message_content(msg).strip()
            and not message_tool_calls(msg)
        ):
            continue
        return msg
    return messages[-1]


def needs_empty_final_continuation(result_state: dict[str, Any]) -> bool:
    """Detect provider/model runs that end right after a tool result.

    LangGraph can finish a stream with the last persisted root message being a
    ToolMessage and no assistant content. In the UI that looks like a stuck run:
    the backend completed, but there is no final answer to render. Continue the
    same thread once or twice so the model can consume that tool result.
    """

    if extract_final_content(result_state):
        return False
    msg = last_message(result_state)
    return message_type(msg) == "tool"


def continuation_prompt_for_empty_final(result_state: dict[str, Any], original_question: str) -> str:
    msg = last_message(result_state)
    tool = message_name(msg)
    content = message_content(msg)
    content_excerpt = compact(content, 7000)
    question = original_question.strip()
    schema_hint = ""
    if tool == "build_subgraph_schema":
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# subgraph_file:") or stripped.startswith("# schema_file:"):
                schema_hint = stripped
                break
    if tool == "build_subgraph_schema":
        return (
            "Continue this KBQA answer from a recovered frontend run.\n"
            f"Original question: {question}\n"
            "The previous run ended immediately after build_subgraph_schema without a final answer.\n"
            f"{schema_hint}\n"
            "Last build_subgraph_schema tool output:\n"
            f"{content_excerpt}\n\n"
            "Call execute_code now using that schema/subgraph file. In execute_code, "
            "store only the requested answer values in result_dict['direct_results'], "
            "then return exactly one boxed JSON answer. Do not stop with progress text."
        )
    if tool == "execute_code":
        return (
            "Continue this KBQA answer from a recovered frontend run.\n"
            f"Original question: {question}\n"
            "The previous run ended immediately after execute_code without a final answer.\n"
            "Last execute_code tool output:\n"
            f"{content_excerpt}\n\n"
            "return exactly one boxed JSON answer, and do not call more tools unless "
            "the result is empty and one recovery attempt is required."
        )
    return (
        "Continue this KBQA answer from a recovered frontend run.\n"
        f"Original question: {question}\n"
        f"The previous run ended immediately after tool `{tool}` without a final answer.\n"
        "Last tool output:\n"
        f"{content_excerpt}\n\n"
        "Consume the last tool result, call the "
        "next required KBQA tool if needed, and return exactly one boxed JSON answer."
    )


def boxed_answer_from_execute_tool(result_state: dict[str, Any]) -> str:
    messages = result_state.get("messages", []) if isinstance(result_state, dict) else []
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if message_type(msg) != "tool" or message_name(msg) != "execute_code":
            continue
        content = message_content(msg)
        for line in content.splitlines():
            match = re.match(r"^\s*ANSWER\s*:\s*(.+?)\s*$", line)
            if not match:
                continue
            raw = match.group(1).strip()
            try:
                parsed = ast.literal_eval(raw)
            except Exception:
                try:
                    parsed = json.loads(raw)
                except Exception:
                    parsed = raw
            if isinstance(parsed, (list, tuple, set)):
                values = [str(item) for item in parsed if item is not None]
            elif parsed is None:
                values = []
            else:
                values = [str(parsed)]
            return "\\boxed{" + json.dumps(values, ensure_ascii=False) + "}"
    return ""


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    await websocket.accept()
    try:
        session = STORE.get(session_id)
    except HTTPException:
        session = STORE.create()

    await websocket.send_text(json.dumps({"type": "history", "session": session}, ensure_ascii=False))

    try:
        while True:
            raw = await websocket.receive_text()
            payload = json.loads(raw)
            if payload.get("type") == "chat":
                await handle_chat(
                    websocket,
                    session["id"],
                    str(payload.get("content", "")),
                    str(payload.get("graph_id", "") or ""),
                )
            elif payload.get("type") == "history":
                await websocket.send_text(
                    json.dumps({"type": "history", "session": STORE.get(session["id"])}, ensure_ascii=False)
                )
    except WebSocketDisconnect:
        return


async def handle_chat(websocket: WebSocket, session_id: str, content: str, graph_id: str = "") -> None:
    content = content.strip()
    if not content:
        return

    session = STORE.get(session_id)
    user_message = ui_message("user", content, graph_id=graph_id)
    session["messages"].append(user_message)
    user_count = sum(1 for item in session["messages"] if item.get("role") == "user")
    if user_count == 1:
        session["title"] = content[:42] + ("..." if len(content) > 42 else "")
    STORE.save(session)

    await websocket.send_text(json.dumps({"type": "message", "message": user_message}, ensure_ascii=False))

    ctx = RuntimeContext(agent_id=AGENT_ID, harness_root=".", max_steps=AGENT_CFG.max_steps)
    lifecycle = RunLifecycle(ctx)
    lifecycle.start()
    os.environ["HARNESS_ROOT"] = str(ROOT)
    os.environ["HARNESS_AGENT_ID"] = AGENT_ID
    os.environ["HARNESS_RUN_DIR"] = str(Path(ctx.run_dir).resolve())

    stream_id = f"assistant-{ctx.run_id}"
    await websocket.send_text(
        json.dumps(
            {
                "type": "run_start",
                "run_id": ctx.run_id,
                "stream_id": stream_id,
                "run_dir": str(ctx.run_dir),
                "model": AGENT_CFG.model.model_id,
                "graph_id": graph_id,
            },
            ensure_ascii=False,
        )
    )

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit(event: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def run_agent_thread() -> None:
        try:
            # Resolve (and build if needed) the graph env for this specific request.
            # Different graph scopes build in parallel; same scope serialised per-scope.
            src_env, scope = resolve_scope_env(graph_id)
            os.environ["KBQA_GENERAL_ENV"] = str(src_env)
            # Bind the env to this logical call (ContextVar) AND to the worker
            # thread (threading.local fallback) so tool calls use the right
            # SQLite/ChromaDB. We set both because the executor's worker threads
            # are reused across concurrent runs - a contextvar set inside a
            # previous run would otherwise leak here.
            from workspaces.deepagents_kbqa_general.tools_impl.build_subgraph_schema import (
                set_thread_env,
                _tl_env as _thread_env_fallback,
            )
            set_thread_env(src_env)
            _thread_env_fallback.value = str(src_env)
            emit({"type": "graph_scope", "scope": scope})
            if os.environ.get("FRONTEND_MOCK_AGENT") == "1":
                result = run_mock_agent(content, emit)
            else:
                result = run_streaming_agent(content, session["thread_id"], str(ctx.run_dir), emit, src_env)
                continuation_count = 0
                while (
                    continuation_count < EMPTY_FINAL_CONTINUATIONS
                    and needs_empty_final_continuation(result)
                ):
                    continuation_count += 1
                    prompt = continuation_prompt_for_empty_final(result, content)
                    continuation_thread_id = (
                        f"{session['thread_id']}-continue-{ctx.run_id}-{continuation_count}"
                    )
                    result = run_streaming_agent(
                        prompt,
                        continuation_thread_id,
                        str(ctx.run_dir),
                        emit,
                        src_env,
                    )
            emit({"type": "_done", "result": result})
        except Exception as exc:  # noqa: BLE001 - streamed to UI as a user-facing error.
            emit({"type": "_error", "error": f"{type(exc).__name__}: {exc}"})

    future = loop.run_in_executor(EXECUTOR, run_agent_thread)
    final_content = ""
    streamed_text = ""
    result_state: dict[str, Any] = {}
    run_started_at = time.monotonic()
    last_event_at = run_started_at
    future_done_at: float | None = None
    run_error: str | None = None

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                now = time.monotonic()
                if future.done():
                    if future_done_at is None:
                        future_done_at = now
                        await asyncio.sleep(0)
                        continue
                    if now - future_done_at <= 10:
                        await asyncio.sleep(0)
                        continue
                    run_error = "Agent thread ended without returning a final response."
                elif RUN_HARD_TIMEOUT_SECONDS and now - run_started_at > RUN_HARD_TIMEOUT_SECONDS:
                    run_error = (
                        f"Agent run exceeded {RUN_HARD_TIMEOUT_SECONDS}s and was stopped. "
                        "Try a narrower question or restart the frontend if a prior run is stuck."
                    )
                elif RUN_IDLE_TIMEOUT_SECONDS and now - last_event_at > RUN_IDLE_TIMEOUT_SECONDS:
                    run_error = (
                        f"Agent produced no updates for {RUN_IDLE_TIMEOUT_SECONDS}s and was stopped. "
                        "This usually means the model/provider stalled after a tool call."
                    )
                if not run_error:
                    continue
                session = STORE.get(session_id)
                error_message = ui_message("system", run_error, tone="error")
                session["messages"].append(error_message)
                STORE.save(session)
                await websocket.send_text(json.dumps({"type": "error", "message": error_message}, ensure_ascii=False))
                future.cancel()
                break

            last_event_at = time.monotonic()

            if event["type"] == "assistant_delta":
                delta = event.get("content", "")
                streamed_text += delta
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "assistant_delta",
                            "id": stream_id,
                            "delta": delta,
                            "agent": event.get("agent") or AGENT_ID,
                            "model_message_id": event.get("message_id", ""),
                            "replace": bool(event.get("replace")),
                        },
                        ensure_ascii=False,
                    )
                )
                continue

            if event["type"] == "reasoning_delta":
                # Live thinking tokens: stream to the UI but do not persist them
                # into the session transcript. Dequeuing already refreshed
                # last_event_at above, so this also keeps the idle watchdog calm
                # during long reasoning phases.
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "reasoning_delta",
                            "id": stream_id,
                            "delta": event.get("content", ""),
                            "agent": event.get("agent") or AGENT_ID,
                            "replace": bool(event.get("replace")),
                            "reasoning_epoch": event.get("reasoning_epoch", 0),
                            "model_message_id": event.get("message_id", ""),
                        },
                        ensure_ascii=False,
                    )
                )
                continue

            if event["type"] == "reasoning_reset":
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "reasoning_reset",
                            "id": stream_id,
                            "agent": event.get("agent") or AGENT_ID,
                            "reasoning_epoch": event.get("reasoning_epoch", 0),
                        },
                        ensure_ascii=False,
                    )
                )
                continue

            session = STORE.get(session_id)

            if event["type"] == "_done":
                result_state = event.get("result", {}) or {}
                final_content = extract_final_content(result_state)
                if not final_content and "\\boxed{" in streamed_text:
                    final_content = streamed_text
                if final_content:
                    assistant_message = ui_message("assistant", final_content, id=stream_id, agent=AGENT_ID)
                    session["messages"].append(assistant_message)
                    STORE.save(session)
                    await websocket.send_text(
                        json.dumps({"type": "assistant_final", "message": assistant_message}, ensure_ascii=False)
                    )
                else:
                    msg = last_message(result_state)
                    tool = message_name(msg) or "tool"
                    run_error = (
                        f"Agent run ended after {tool} without a final answer. "
                        "The backend sent run_done so the UI can recover; retry the question if needed."
                    )
                    error_message = ui_message("system", run_error, tone="error")
                    session["messages"].append(error_message)
                    STORE.save(session)
                    await websocket.send_text(
                        json.dumps({"type": "error", "message": error_message}, ensure_ascii=False)
                    )
                break

            if event["type"] == "_error":
                run_error = event["error"]
                error_message = ui_message("system", event["error"], tone="error")
                session["messages"].append(error_message)
                STORE.save(session)
                await websocket.send_text(json.dumps({"type": "error", "message": error_message}, ensure_ascii=False))
                break

            event_message = event_to_message(event)
            session["messages"].append(event_message)
            STORE.save(session)
            await websocket.send_text(json.dumps({"type": "event", "message": event_message}, ensure_ascii=False))

        if future.done():
            try:
                await future
            except Exception as exc:  # noqa: BLE001 - already surfaced to the UI when possible.
                if not run_error:
                    run_error = f"{type(exc).__name__}: {exc}"
        else:
            future.cancel()
    finally:
        try:
            workspace_dir = str(_resolve_path(AGENT_CFG.workspace, str(ROOT)))
            system_prompt = _load_prompt(AGENT_CFG, workspace_dir, str(ROOT))
            output_path = ctx.run_dir / "messages.jsonl"
            if isinstance(result_state, dict) and result_state.get("messages"):
                record_run(
                    result_state["messages"],
                    output_path,
                    metadata={"run_id": ctx.run_id, "agent_id": AGENT_ID, "ui_session_id": session_id},
                    system_prompt=system_prompt,
                )
        except Exception:
            pass

        ctx.step()
        lifecycle.update_step()
        lifecycle.finish(run_error)
        try:
            await websocket.send_text(json.dumps({"type": "run_done", "run_id": ctx.run_id}, ensure_ascii=False))
        except Exception:
            pass


def run_mock_agent(message: str, emit) -> dict[str, Any]:
    emit({"type": "tool_call", "agent": AGENT_ID, "tool": "mock_runtime", "args": {"message": message}})
    time.sleep(0.05)
    emit({"type": "tool_event", "event": "tool_end", "agent": AGENT_ID, "tool": "mock_runtime", "output": "Mock tool completed."})
    final = f"Mock streaming response from {AGENT_ID}. Your message was: {message}"
    for chunk in final.split(" "):
        emit({"type": "assistant_delta", "agent": AGENT_ID, "content": chunk + " ", "message_id": "mock-final", "replace": False})
        time.sleep(0.01)
    return {"final_content": final}


_AGENT_SINGLETON = None
_AGENT_BUILD_LOCK = threading.Lock()


def get_cached_agent():
    """Build the deep agent once and reuse it across conversations.

    The agent itself is stateless; per-conversation memory lives in the
    checkpointer keyed by thread_id, and per-request graph scope lives in a
    thread-local env. Rebuilding it on every turn only wasted time reconnecting
    the checkpoint store and recompiling the graph.
    """
    global _AGENT_SINGLETON
    if _AGENT_SINGLETON is None:
        with _AGENT_BUILD_LOCK:
            if _AGENT_SINGLETON is None:
                _AGENT_SINGLETON = build_agent(AGENT_CFG, ".", registry=REGISTRY)
    return _AGENT_SINGLETON


def run_streaming_agent(message: str, thread_id: str, run_dir: str, emit, graph_env: Path | str | None = None) -> dict[str, Any]:
    os.environ["HARNESS_ROOT"] = str(ROOT)
    os.environ["HARNESS_AGENT_ID"] = AGENT_ID
    os.environ["HARNESS_RUN_DIR"] = str(Path(run_dir).resolve())
    graph_env_str = str(graph_env) if graph_env else ""
    if graph_env_str:
        from workspaces.deepagents_kbqa_general.tools_impl.build_subgraph_schema import bind_thread_env
        bind_thread_env(thread_id or "default", graph_env_str)

    agent = get_cached_agent()
    config = {
        "configurable": {
            "thread_id": thread_id or "default",
            "kbqa_general_env": graph_env_str,
        }
    }
    last_values: dict[str, Any] = {}
    root_prev_count: int | None = None
    seen_message_ids: set[str] = set()
    replace_next_model_delta = True
    last_reasoning_model_key: str | None = None
    reasoning_has_content = False
    reasoning_reset_pending = False
    reasoning_epoch = 0
    reasoning_buffer = ""

    for item in agent.stream(
        {"messages": [HumanMessage(content=message)]},
        config=config,
        stream_mode=["messages", "values"],
        subgraphs=True,
    ):
        if not isinstance(item, tuple) or len(item) != 3:
            continue
        namespace, mode, data = item
        is_root = namespace == ()

        if mode == "messages":
            chunk, meta = data
            if meta.get("langgraph_node") != "model":
                continue
            agent_name = meta.get("lc_agent_name") or meta.get("name") or AGENT_ID
            # 优先从 additional_kwargs 拿增量 token：DeepSeek thinking 模式下，
            # chunk.content 是累积的（包含历史），同时 chunk.additional_kwargs 里
            # 'reasoning_content' / 'content' 是 incremental delta。
            # 走 additional_kwargs 可以严格区分 reasoning vs 正文，避免把正文
            # 当成 reasoning 推到前端"Model thinking"块里。
            kwargs = getattr(chunk, "additional_kwargs", None) or {}
            reasoning_inc = ""
            content_inc = ""
            if isinstance(kwargs, dict):
                r = kwargs.get("reasoning_content")
                if isinstance(r, str):
                    reasoning_inc = r
                c = kwargs.get("content")
                if isinstance(c, str):
                    content_inc = c
            # 兜底：如果 additional_kwargs 拿不到增量，再用 chunk.content（注意是累积值）
            if not reasoning_inc and not content_inc:
                text = content_to_text(getattr(chunk, "content", ""))
                if text:
                    content_inc = text
                else:
                    reasoning_inc = reasoning_from_chunk(chunk)
            if reasoning_inc:
                # Model thinking is displayed per tool-planning turn. After an
                # AI/tool message has completed, the next reasoning token belongs
                # to a new turn, so clear the previous thinking before streaming
                # the new one. The metadata key is only a fallback for model-step
                # changes that do not surface as completed messages first.
                model_step_key = "|".join(
                    part for part in [
                        repr(namespace),
                        str(meta.get("langgraph_step") or ""),
                        str(meta.get("run_id") or ""),
                    ] if part
                )
                should_reset_reasoning = reasoning_has_content and (
                    reasoning_reset_pending
                    or (last_reasoning_model_key is not None and last_reasoning_model_key != model_step_key)
                )
                replace_reasoning = False
                if should_reset_reasoning:
                    reasoning_epoch += 1
                    emit({"type": "reasoning_reset", "agent": agent_name, "reasoning_epoch": reasoning_epoch})
                    previous_reasoning = reasoning_buffer
                    reasoning_buffer = ""
                    reasoning_has_content = False
                    replace_reasoning = True
                    if previous_reasoning and reasoning_inc.startswith(previous_reasoning):
                        reasoning_inc = reasoning_inc[len(previous_reasoning):]
                elif reasoning_buffer and reasoning_inc.startswith(reasoning_buffer):
                    reasoning_inc = reasoning_inc[len(reasoning_buffer):]
                reasoning_reset_pending = False
                last_reasoning_model_key = model_step_key
                reasoning_has_content = True
                if reasoning_inc:
                    reasoning_buffer = f"{reasoning_buffer}{reasoning_inc}"
                    emit(
                        {
                            "type": "reasoning_delta",
                            "agent": agent_name,
                            "content": reasoning_inc,
                            "message_id": model_step_key,
                            "reasoning_epoch": reasoning_epoch,
                            "replace": replace_reasoning,
                        }
                    )
            if content_inc:
                message_id = "|".join(
                    part
                    for part in [
                        str(getattr(chunk, "id", "") or ""),
                        repr(namespace),
                        str(meta.get("langgraph_step") or ""),
                        str(meta.get("checkpoint_ns") or ""),
                        str(meta.get("langgraph_node") or "model"),
                        str(meta.get("run_id") or ""),
                    ]
                    if part
                )
                emit(
                    {
                        "type": "assistant_delta",
                        "agent": agent_name,
                        "content": content_inc,
                        "message_id": message_id,
                        "replace": replace_next_model_delta,
                    }
                )
                replace_next_model_delta = False
            continue

        if mode != "values" or not isinstance(data, dict):
            continue

        messages = data.get("messages", []) or []
        if is_root:
            last_values = data
            if root_prev_count is None:
                root_prev_count = len(messages)
                for msg in messages:
                    msg_id = getattr(msg, "id", "")
                    if msg_id:
                        seen_message_ids.add(msg_id)
                continue
            new_messages = messages[root_prev_count:]
            root_prev_count = len(messages)
        else:
            new_messages = messages

        for msg in new_messages:
            process_stream_message(msg, seen_message_ids, emit)
            # A completed graph message (AI/tool) means any future model token is
            # a fresh model turn. This is more reliable than provider-specific
            # chunk ids, especially for reasoning_content streams.
            if getattr(msg, "type", "") in {"ai", "tool"}:
                replace_next_model_delta = True
                reasoning_reset_pending = reasoning_has_content

    if not last_values.get("messages"):
        try:
            state = agent.get_state(config)
            if state:
                last_values = dict(state.values)
        except Exception:
            pass
    return last_values if last_values else {"messages": []}


def process_stream_message(msg: Any, seen_message_ids: set[str], emit) -> None:
    msg_id = getattr(msg, "id", "") or f"{getattr(msg, 'type', 'msg')}:{hash(content_to_text(getattr(msg, 'content', '')))}"
    if msg_id in seen_message_ids:
        return
    seen_message_ids.add(msg_id)

    msg_type = getattr(msg, "type", "")
    agent = getattr(msg, "name", "") or AGENT_ID
    if msg_type == "ai":
        content = content_to_text(getattr(msg, "content", ""))
        if content:
            emit({"type": "model_output", "agent": agent, "content": content, "message_id": msg_id})
        for call in getattr(msg, "tool_calls", None) or []:
            emit(
                {
                    "type": "tool_call",
                    "agent": agent,
                    "tool": call.get("name", "unknown"),
                    "args": call.get("args", {}),
                    "call_id": call.get("id", ""),
                }
            )
    elif msg_type == "tool":
        emit(
            {
                "type": "tool_event",
                "event": "tool_end",
                "agent": agent,
                "tool": getattr(msg, "name", "tool") or "tool",
                "output": content_to_text(getattr(msg, "content", "")),
            }
        )


def extract_final_content(result_state: dict[str, Any]) -> str:
    if result_state.get("final_content"):
        return str(result_state["final_content"])
    msg = last_message(result_state)
    if message_type(msg) == "ai" and not message_tool_calls(msg):
        content = message_content(msg)
        if content:
            return content
    return boxed_answer_from_execute_tool(result_state)


def event_to_message(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event["type"]
    if event_type == "graph_scope":
        scope = event.get("scope", {}) or {}
        label = scope.get("graph_name") or "All graphs"
        mode = scope.get("mode") or "all"
        return ui_message(
            "event",
            compact(scope, 2000),
            kind="graph_scope",
            title=f"Graph Scope · {label} ({mode})",
            agent=AGENT_ID,
        )
    if event_type == "graph_wait":
        return ui_message(
            "event",
            event.get("message", "Waiting for graph runtime."),
            kind="graph_wait",
            title="Graph Runtime Queue",
            agent=AGENT_ID,
        )
    if event_type == "model_output":
        return ui_message(
            "event",
            event["content"],
            kind="model_output",
            title="Model output",
            agent=event.get("agent") or AGENT_ID,
            model_message_id=event.get("message_id", ""),
        )
    if event_type == "tool_call":
        return ui_message(
            "event",
            compact(event.get("args", {}), 3000),
            kind="tool_call",
            title=f"Calling {event.get('tool', 'tool')}",
            agent=event.get("agent") or AGENT_ID,
            tool=event.get("tool", ""),
        )
    if event_type == "tool_event":
        label = event.get("event", "tool_event")
        output = event.get("output", event.get("input", ""))
        extra: dict = {}

        # For execute_code tool_end: extract the full rendered script from the cache file,
        # attach it as `full_code`, and strip the hidden path comment from the displayed output.
        if event.get("tool") == "execute_code" and label == "tool_end" and output:
            clean_lines = []
            for line in output.splitlines():
                if line.strip().startswith("# __exec_script__:"):
                    rel_path = line.strip()[len("# __exec_script__:"):].strip()
                    full_code = read_marked_exec_script(rel_path)
                    if full_code:
                        extra["full_code"] = full_code
                else:
                    clean_lines.append(line)
            output = "\n".join(clean_lines).strip()

        return ui_message(
            "event",
            compact(output, 5000),
            kind=label,
            title=f"{label.replace('_', ' ').title()} · {event.get('tool') or event.get('agent') or 'tool'}",
            agent=event.get("agent") or AGENT_ID,
            tool=event.get("tool", ""),
            **extra,
        )
    return ui_message("event", compact(event), kind=event_type, title=event_type)


def port_listener_pids(port: int) -> list[int]:
    """Return local listener PIDs for a TCP port using the macOS/Linux lsof tool."""

    try:
        result = subprocess.run(
            ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    pids: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def process_command(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def is_stale_frontend_process(pid: int) -> bool:
    if pid == os.getpid():
        return False
    command = process_command(pid)
    script_path = str(FRONTEND_SCRIPT)
    relative_script = "workspaces/deepagents_kbqa_general/frontend/app.py"
    return script_path in command or relative_script in command


def wait_until_port_free(port: int, timeout_seconds: float = 4.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not port_listener_pids(port):
            return True
        time.sleep(0.15)
    return not port_listener_pids(port)


def stop_stale_frontend_processes(port: int) -> None:
    pids = port_listener_pids(port)
    if not pids:
        return

    stale_pids = [pid for pid in pids if is_stale_frontend_process(pid)]
    foreign_pids = [pid for pid in pids if pid not in stale_pids]
    if foreign_pids:
        details = "; ".join(f"{pid}: {process_command(pid) or 'unknown command'}" for pid in foreign_pids)
        raise RuntimeError(
            f"Port {port} is occupied by a non-Code-On-Graph process. "
            f"Stop it manually or set KBQA_UI_PORT/PORT to another value. Occupiers: {details}"
        )

    for pid in stale_pids:
        print(f"Stopping stale KnowCode KC-Agent process on port {port}: PID {pid}", flush=True)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue

    if wait_until_port_free(port):
        return

    for pid in stale_pids:
        try:
            print(f"Force stopping stale Knowledge Computing Agent (KC-Agent) process: PID {pid}", flush=True)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue

    if not wait_until_port_free(port, timeout_seconds=2.0):
        raise RuntimeError(f"Port {port} is still occupied after stopping stale frontend processes.")


def main() -> None:
    import uvicorn

    port = int(os.environ.get("KBQA_UI_PORT", os.environ.get("PORT", "8093")))
    if os.environ.get("KBQA_UI_KILL_STALE", "1") != "0":
        stop_stale_frontend_processes(port)
    print("\nKnowledge Computing Agent (KC-Agent)", flush=True)
    print(f"  http://127.0.0.1:{port}", flush=True)
    print(f"  sessions: {SESSION_DIR}\n", flush=True)
    if os.environ.get("KBQA_PREWARM", "1") != "0":
        threading.Thread(target=prewarm_runtime, name="kbqa-prewarm", daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


def prewarm_runtime() -> None:
    """Warm the default scope so the first user question is not slow.

    Loads the BGE model + ChromaDB index for the default ('All graphs') scope
    and pre-builds the cached agent. Runs in a background thread so it never
    blocks server startup; failures are non-fatal (lazy loading still works).
    """
    try:
        t0 = time.time()
        from workspaces.deepagents_kbqa_general.tools_impl.build_subgraph_schema import (
            prewarm_models,
            set_thread_env,
        )

        src_env, scope = resolve_scope_env("")
        set_thread_env(src_env)
        prewarm_models()
        get_cached_agent()
        # 预热 summary 缓存，让第一次切/刷新图谱时 0 延迟
        from workspaces.deepagents_kbqa_general.code.graph_runtime import (
            summarize_graph,
            list_graphs,
        )
        for g in list_graphs():
            gid = g.get("id", "")
            if not gid:
                continue
            try:
                summarize_graph(gid)
            except Exception as exc:  # noqa: BLE001
                print(f"[prewarm] summarize {gid} skipped: {exc}", flush=True)
        print(f"[prewarm] runtime ready in {time.time() - t0:.1f}s ({src_env.name})", flush=True)
    except Exception as exc:  # noqa: BLE001 - prewarm is best-effort.
        print(f"[prewarm] skipped: {type(exc).__name__}: {exc}", flush=True)


if __name__ == "__main__":
    main()
