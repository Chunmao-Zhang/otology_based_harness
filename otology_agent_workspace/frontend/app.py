#!/usr/bin/env python3
"""Ontology QA Agent frontend.

Run from the repository root:
    PYTHONPATH=. python3 otology_agent_workspace/frontend/app.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from harness.ontology.schema_service import (
    confirm_schema,
    generate_schema_from_form,
    schema_to_form,
)
from harness.ontology.schema_utils import parse_schema

AGENT_ID = "ontology_coordinator"
UI_BRAND = "Ontology QA Agent"
STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_DIR = ROOT / "outputs" / "ontology_coordinator" / "frontend_sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = ROOT / "otology_agent_workspace" / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR = ROOT / "runs" / "ontology_workspace_runs"
ALLOWED_UPLOAD_SUFFIXES = {".csv", ".txt", ".md"}
MOCK_MODE = os.environ.get("ONTOLOGY_UI_MOCK", "") == "1"

EXECUTOR = ThreadPoolExecutor(max_workers=4)
_AGENT_LOCK = threading.Lock()
_AGENT_SINGLETON = None

PIPELINE_STAGES = [
    {"id": "clarify", "label": "Clarify Question"},
    {"id": "confirm_problem", "label": "Confirm Question"},
    {"id": "evidence", "label": "Collect Evidence"},
    {"id": "schema_build", "label": "Build Schema"},
    {"id": "schema_judge", "label": "Judge Schema"},
    {"id": "confirm_schema", "label": "Confirm Schema"},
    {"id": "extract", "label": "Extract Data"},
    {"id": "solve", "label": "Solve & Answer"},
]

SUBAGENT_STAGE = {
    "problem_clarifier": "clarify",
    "evidence_collector": "evidence",
    "schema_builder": "schema_build",
    "schema_judger": "schema_judge",
    "data_extractor": "extract",
    "workspace_solver": "solve",
}

STAGE_RUNNING_TEXT = {
    "clarify": "Analyzing and clarifying the question…",
    "evidence": "Gathering evidence (reading uploads, searching the web if needed)…",
    "schema_build": "Building the ontology schema…",
    "schema_judge": "Judging whether the schema can answer the question…",
    "extract": "Extracting data against the confirmed schema…",
    "solve": "Executing code in the workspace to solve the question…",
}


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


def fresh_stages() -> list[dict[str, str]]:
    return [{"id": s["id"], "label": s["label"], "status": "pending"} for s in PIPELINE_STAGES]


class SessionStore:
    def create(self) -> dict[str, Any]:
        session_id = uuid4().hex[:12]
        session = {
            "id": session_id,
            "title": "New chat",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "thread_id": f"ontology-ui-{session_id}",
            "messages": [],
            "stages": fresh_stages(),
        }
        write_json(session_path(session_id), session)
        return session

    def list(self) -> list[dict[str, Any]]:
        items = []
        for path in SESSION_DIR.glob("*.json"):
            try:
                data = read_json(path)
            except Exception:
                continue
            items.append({
                "id": data.get("id", path.stem),
                "title": data.get("title", "New chat"),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
                "message_count": sum(1 for m in data.get("messages", []) if m.get("role") in ("user", "assistant")),
            })
        items.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return items

    def get(self, session_id: str) -> dict[str, Any]:
        path = session_path(session_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Session not found")
        data = read_json(path)
        if "stages" not in data:
            data["stages"] = fresh_stages()
        return data

    def save(self, session: dict[str, Any]) -> None:
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
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"), media_type="text/html")


@app.get("/api/health")
async def health():
    model_id = ""
    try:
        cfg = read_json(ROOT / "harness.json")
        model_id = str(cfg.get("defaults", {}).get("model", ""))
    except Exception:
        pass
    return {
        "ok": True,
        "brand": UI_BRAND,
        "agent": AGENT_ID,
        "model": model_id,
        "mock": MOCK_MODE,
        "upload_count": len(list_uploads()),
    }


# ─── Uploads ──────────────────────────────────────────────────────────────────

def list_uploads() -> list[dict[str, Any]]:
    items = []
    for path in sorted(UPLOAD_DIR.iterdir() if UPLOAD_DIR.exists() else []):
        if not path.is_file() or path.suffix.lower() not in ALLOWED_UPLOAD_SUFFIXES:
            continue
        stat = path.stat()
        items.append({
            "id": path.name,
            "name": path.name,
            "type": path.suffix.lstrip(".").lower(),
            "size": stat.st_size,
            "uploaded_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        })
    items.sort(key=lambda item: item["uploaded_at"], reverse=True)
    return items


def safe_upload_path(upload_id: str) -> Path:
    name = Path(upload_id).name
    path = (UPLOAD_DIR / name).resolve()
    if UPLOAD_DIR.resolve() not in path.parents:
        raise HTTPException(status_code=400, detail="Invalid upload id")
    return path


@app.get("/api/uploads")
async def uploads_index():
    return {"uploads": list_uploads()}


@app.post("/api/uploads")
async def upload_file(file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(status_code=400, detail="Only csv / txt / md files are supported")
    stem = re.sub(r"[^\w.\-]+", "_", Path(file.filename or "upload").stem)[:60] or "upload"
    target = UPLOAD_DIR / f"{stem}{suffix}"
    counter = 1
    while target.exists():
        target = UPLOAD_DIR / f"{stem}_{counter}{suffix}"
        counter += 1
    target.write_bytes(await file.read())
    return {"ok": True, "upload": {"id": target.name, "name": target.name}}


@app.delete("/api/uploads/{upload_id}")
async def delete_upload(upload_id: str):
    path = safe_upload_path(upload_id)
    if path.exists():
        path.unlink()
    return {"ok": True}


@app.get("/api/uploads/{upload_id}/preview")
async def preview_upload(upload_id: str):
    path = safe_upload_path(upload_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    text = path.read_text(encoding="utf-8", errors="replace")
    return {"ok": True, "name": path.name, "preview": text[:4000]}


# ─── Run artifacts (evidence / schema / results) ──────────────────────────────

def list_run_dirs() -> list[Path]:
    if not RUNS_DIR.exists():
        return []
    dirs = [p for p in RUNS_DIR.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return dirs


def latest_run_with(relative: str) -> Path | None:
    for run_dir in list_run_dirs():
        if (run_dir / relative).exists():
            return run_dir
    return None


@app.get("/api/evidence")
async def evidence_summary():
    run_dir = latest_run_with("intermediate/evidence_manifest.json")
    if run_dir is None:
        return {"ok": True, "run_id": "", "sources": [], "needs_web_search": False}
    try:
        manifest = read_json(run_dir / "intermediate" / "evidence_manifest.json")
    except Exception:
        return {"ok": False, "run_id": run_dir.name, "sources": [], "needs_web_search": False}
    sources = []
    for source in manifest.get("sources", []) or []:
        if not isinstance(source, dict):
            continue
        sources.append({
            "source_id": Path(str(source.get("source_id", ""))).name,
            "source_kind": source.get("source_kind", ""),
            "file_type": source.get("file_type", ""),
            "reason": source.get("reason", ""),
            "url": source.get("url", ""),
        })
    return {
        "ok": True,
        "run_id": run_dir.name,
        "question": manifest.get("question", ""),
        "sources": sources,
        "needs_web_search": bool(manifest.get("needs_web_search", False)),
    }


def schema_payload(run_dir: Path) -> dict[str, Any]:
    confirmed = run_dir / "concepts" / "confirmed_schema.py"
    draft = run_dir / "concepts" / "draft_schema.py"
    path = confirmed if confirmed.exists() else draft
    status = "confirmed" if confirmed.exists() else "draft"
    text = path.read_text(encoding="utf-8")
    form = schema_to_form(schema_text=text)
    return {"ok": True, "run_id": run_dir.name, "status": status, "form": form, "schema_text": text}


@app.get("/api/schema")
async def get_schema():
    run_dir = latest_run_with("concepts/draft_schema.py") or latest_run_with("concepts/confirmed_schema.py")
    if run_dir is None:
        return {"ok": True, "run_id": "", "status": "none", "form": [], "schema_text": ""}
    try:
        return schema_payload(run_dir)
    except Exception as exc:
        return {"ok": False, "run_id": run_dir.name, "status": "error", "error": str(exc), "form": [], "schema_text": ""}


@app.post("/api/schema/form")
async def update_schema_from_form(payload: dict[str, Any]):
    run_id = str(payload.get("run_id", "")).strip()
    form = payload.get("form")
    if not run_id or not isinstance(form, list):
        raise HTTPException(status_code=400, detail="run_id and form are required")
    run_dir = RUNS_DIR / Path(run_id).name
    draft = run_dir / "concepts" / "draft_schema.py"
    if not draft.exists():
        raise HTTPException(status_code=404, detail="Draft schema not found")
    try:
        text = generate_schema_from_form(form, output_path=draft)
        parsed = parse_schema(text)
        if not parsed.valid:
            return {"ok": False, "errors": parsed.errors}
    except Exception as exc:
        return {"ok": False, "errors": [str(exc)]}
    return schema_payload(run_dir)


@app.post("/api/schema/confirm")
async def confirm_schema_endpoint(payload: dict[str, Any]):
    run_id = str(payload.get("run_id", "")).strip()
    if not run_id:
        raise HTTPException(status_code=400, detail="run_id is required")
    run_dir = RUNS_DIR / Path(run_id).name
    draft = run_dir / "concepts" / "draft_schema.py"
    if not draft.exists():
        raise HTTPException(status_code=404, detail="Draft schema not found")
    result = confirm_schema(draft, run_dir / "concepts" / "confirmed_schema.py")
    if not result.get("valid", False):
        return {"ok": False, "errors": result.get("errors", [])}
    return schema_payload(run_dir)


@app.get("/api/results")
async def results_summary():
    run_dir = latest_run_with("intermediate/extraction_report.json")
    report: dict[str, Any] = {}
    answer_sources: list[str] = []
    if run_dir is not None:
        try:
            raw = read_json(run_dir / "intermediate" / "extraction_report.json")
            report = {
                "total_instances": raw.get("total_instances"),
                "total_facts": raw.get("total_facts"),
                "total_relations": raw.get("total_relations"),
                "avg_confidence": raw.get("avg_confidence"),
                "relation_types_used": raw.get("relation_types_used", []),
            }
        except Exception:
            report = {}
        solver_path = run_dir / "intermediate" / "solver_result.json"
        if solver_path.exists():
            try:
                solver = read_json(solver_path)
                refs = solver.get("source_refs") or solver.get("sources") or []
                if isinstance(refs, list):
                    answer_sources = [str(item) for item in refs][:20]
            except Exception:
                pass
    return {
        "ok": True,
        "run_id": run_dir.name if run_dir else "",
        "report": report,
        "answer_sources": answer_sources,
    }


# ─── Sessions ─────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def sessions_index():
    return {"sessions": STORE.list()}


@app.post("/api/sessions")
async def sessions_create():
    session = STORE.create()
    return {"session": session}


@app.get("/api/sessions/{session_id}")
async def sessions_get(session_id: str):
    return {"session": STORE.get(session_id)}


@app.delete("/api/sessions/{session_id}")
async def sessions_delete(session_id: str):
    STORE.delete(session_id)
    return {"ok": True}


# ─── Chat ─────────────────────────────────────────────────────────────────────

def ui_message(role: str, content: str = "", **extra: Any) -> dict[str, Any]:
    message = {"id": uuid4().hex[:10], "role": role, "content": content, "timestamp": now_iso()}
    message.update(extra)
    return message


def set_stage(session: dict[str, Any], stage_id: str, status: str) -> None:
    order = [s["id"] for s in PIPELINE_STAGES]
    if stage_id not in order:
        return
    target_index = order.index(stage_id)
    for stage in session["stages"]:
        index = order.index(stage["id"])
        if index < target_index and stage["status"] in ("pending", "running", "waiting"):
            stage["status"] = "done"
        elif index == target_index:
            stage["status"] = status


def stage_label(stage_id: str) -> str:
    for stage in PIPELINE_STAGES:
        if stage["id"] == stage_id:
            return stage["label"]
    return stage_id


def extract_clarification(text: str) -> dict[str, Any] | None:
    """Pull a structured {problem, steps} out of a clarification reply."""
    try:
        from harness.ontology.json_contract import extract_json_object

        data = extract_json_object(text)
        problem = data.get("problem")
        steps = data.get("steps")
        if isinstance(problem, str) and problem.strip() and isinstance(steps, list):
            cleaned = [str(item).strip() for item in steps if str(item).strip()]
            if cleaned:
                return {"problem": problem.strip(), "steps": cleaned}
    except Exception:
        pass
    problem = ""
    steps: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        match = re.match(r"^\*?\*?(?:Question|\u95ee\u9898)\*?\*?[:\uff1a]\s*(.+)$", stripped)
        if match and not problem:
            problem = match.group(1).strip()
            continue
        match = re.match(r"^\d+[.\u3001)]\s*(.+)$", stripped)
        if match:
            steps.append(match.group(1).strip())
    if problem and steps:
        return {"problem": problem, "steps": steps}
    return None


def detect_gate(text: str) -> str:
    """Infer which confirmation gate the coordinator is waiting on."""
    lowered = text.lower()
    if "schema" in lowered and ("确认" in text or "confirm" in lowered):
        return "confirm_schema"
    if "确认" in text or "confirm" in lowered:
        return "confirm_problem"
    return ""


def get_cached_agent():
    global _AGENT_SINGLETON
    if _AGENT_SINGLETON is None:
        with _AGENT_LOCK:
            if _AGENT_SINGLETON is None:
                from harness.agents.agent_loop import build_agent
                from harness.agents.registry import AgentRegistry
                from harness.config import load_config

                cfg = load_config(str(ROOT / "harness.json"))
                registry = AgentRegistry(cfg)
                agent_cfg = registry.get(AGENT_ID)
                _AGENT_SINGLETON = (build_agent(agent_cfg, ".", registry=registry), agent_cfg)
    return _AGENT_SINGLETON


def run_real_agent(message: str, thread_id: str, emit) -> str:
    from langchain_core.messages import HumanMessage

    agent, agent_cfg = get_cached_agent()
    os.environ["HARNESS_ROOT"] = str(ROOT)
    os.environ["HARNESS_AGENT_ID"] = AGENT_ID

    final_content = ""
    config = {"configurable": {"thread_id": thread_id or "default"}}
    for item in agent.stream(
        {"messages": [HumanMessage(content=message)]},
        config=config,
        stream_mode=["values"],
        subgraphs=True,
    ):
        if not isinstance(item, tuple) or len(item) != 3:
            continue
        namespace, _mode, data = item
        messages = data.get("messages", []) if isinstance(data, dict) else []
        if not messages:
            continue
        last = messages[-1]
        msg_type = getattr(last, "type", "")
        tool_calls = getattr(last, "tool_calls", None) or []
        for call in tool_calls:
            if call.get("name") != "task":
                continue
            args = call.get("args", {}) or {}
            subagent = str(args.get("subagent_type", "") or args.get("agent", ""))
            stage = SUBAGENT_STAGE.get(subagent, "")
            if stage:
                emit({"type": "stage", "stage": stage, "status": "running"})
        if namespace == () and msg_type == "ai":
            content = getattr(last, "content", "") or ""
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if content and not tool_calls:
                final_content = content
    return final_content


MOCK_SCHEMA = '''from typing import List, Optional


class Company:  # entity_type: Organization
    _id: str
    name: str
    country: Optional[str]
    operates_in_industry: List["Industry"]


class Industry:  # entity_type: BusinessDomain
    _id: str
    name: str
    operates_in_industry_r: List["Company"]  # reverse
'''


def run_mock_agent(message: str, session: dict[str, Any], emit) -> str:
    """Deterministic walkthrough of the pipeline for local UI testing."""
    stages = {s["id"]: s["status"] for s in session["stages"]}
    if stages.get("confirm_problem") == "waiting":
        run_id = f"mock{uuid4().hex[:6]}"
        run_dir = RUNS_DIR / run_id
        (run_dir / "concepts").mkdir(parents=True, exist_ok=True)
        (run_dir / "intermediate").mkdir(parents=True, exist_ok=True)
        (run_dir / "concepts" / "draft_schema.py").write_text(MOCK_SCHEMA, encoding="utf-8")
        (run_dir / "intermediate" / "evidence_manifest.json").write_text(json.dumps({
            "question": "Which companies in the US do data analytics?",
            "needs_web_search": False,
            "sources": [{"source_id": "company_sample.csv", "source_kind": "upload", "file_type": "csv",
                         "reason": "Sample company table with company name, country and industry fields"}],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        for stage, pause in (("evidence", 1.2), ("schema_build", 1.6), ("schema_judge", 1.2)):
            emit({"type": "stage", "stage": stage, "status": "running"})
            time.sleep(pause)
        emit({"type": "stage", "stage": "confirm_schema", "status": "waiting"})
        return (
            "The draft schema is ready. Judgment: the question is answerable (coverage 0.92).\n\n"
            "| Head | Relation | Tail |\n|---|---|---|\n"
            "| Company | operates_in_industry | Industry |\n\n"
            "Review and edit it in the Schema Studio on the right. Once you confirm, I will continue with data extraction."
        )
    if stages.get("confirm_schema") == "waiting":
        run_dir = latest_run_with("concepts/draft_schema.py")
        if run_dir is not None:
            confirm_schema(run_dir / "concepts" / "draft_schema.py", run_dir / "concepts" / "confirmed_schema.py")
            (run_dir / "intermediate" / "extraction_report.json").write_text(json.dumps({
                "total_instances": 18, "total_facts": 42, "total_relations": 16,
                "relation_types_used": ["operates_in_industry"], "avg_confidence": 0.87,
            }, indent=2), encoding="utf-8")
        emit({"type": "stage", "stage": "extract", "status": "running"})
        time.sleep(1.6)
        emit({"type": "stage", "stage": "solve", "status": "running"})
        time.sleep(1.6)
        emit({"type": "stage", "stage": "solve", "status": "done"})
        return (
            "Data extraction and solving are complete.\n\nData analytics companies in the US include: Palantir, Databricks, and Snowflake.\n\n"
            "Source: company_sample.csv (18 instances, 42 facts)."
        )
    emit({"type": "stage", "stage": "clarify", "status": "running"})
    time.sleep(1.4)
    emit({"type": "stage", "stage": "confirm_problem", "status": "waiting"})
    return (
        "Here is my understanding of the question. Please confirm:\n\n"
        "**Question**: List data analytics companies operating in the US, including company names and their sub-domains.\n\n"
        "**Solution steps**:\n1. Build Company and Industry entities and their relations\n2. Search for and extract matching data\n3. Return the list of company names and domains\n\n"
        "Reply \"Confirm\" to continue, or tell me what needs to change."
    )


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
                    list(payload.get("upload_ids", []) or []),
                )
            elif payload.get("type") == "confirm_problem":
                problem = str(payload.get("problem", "")).strip()
                steps = [str(item).strip() for item in (payload.get("steps") or []) if str(item).strip()]
                if not problem or not steps:
                    error_message = ui_message("system", "The problem statement and at least one step are required.", tone="error")
                    await websocket.send_text(json.dumps({"type": "error", "message": error_message}, ensure_ascii=False))
                    continue
                current = STORE.get(session["id"])
                current["clarification"] = {"problem": problem, "steps": steps, "status": "confirmed"}
                STORE.save(current)
                composed = (
                    "I confirm the clarified problem.\n\n"
                    f"**Question**: {problem}\n\n"
                    "**Solution steps**:\n"
                    + "\n".join(f"{index + 1}. {step}" for index, step in enumerate(steps))
                )
                await handle_chat(websocket, session["id"], composed, [])
            elif payload.get("type") == "history":
                await websocket.send_text(
                    json.dumps({"type": "history", "session": STORE.get(session["id"])}, ensure_ascii=False)
                )
    except WebSocketDisconnect:
        return


async def handle_chat(websocket: WebSocket, session_id: str, content: str, upload_ids: list[str]) -> None:
    content = content.strip()
    if not content:
        return

    session = STORE.get(session_id)
    upload_names = [Path(str(item)).name for item in upload_ids if str(item).strip()]
    user_message = ui_message("user", content, uploads=upload_names)
    session["messages"].append(user_message)
    if sum(1 for item in session["messages"] if item.get("role") == "user") == 1:
        session["title"] = content[:42] + ("..." if len(content) > 42 else "")
    STORE.save(session)
    await websocket.send_text(json.dumps({"type": "message", "message": user_message}, ensure_ascii=False))
    await websocket.send_text(json.dumps({"type": "run_start"}, ensure_ascii=False))

    agent_input = content
    if upload_names:
        paths = [str(Path("otology_agent_workspace/data/uploads") / name) for name in upload_names]
        agent_input = f"{content}\n\nupload_paths: {json.dumps(paths, ensure_ascii=False)}"

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit(event: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def run_agent_thread() -> None:
        try:
            if MOCK_MODE:
                final = run_mock_agent(agent_input, session, emit)
            else:
                final = run_real_agent(agent_input, session["thread_id"], emit)
            emit({"type": "_done", "final": final})
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a friendly error.
            emit({"type": "_error", "error": f"{type(exc).__name__}: {exc}"})

    future = loop.run_in_executor(EXECUTOR, run_agent_thread)
    run_error: str | None = None

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=900)
            except asyncio.TimeoutError:
                run_error = "The run timed out. Please retry or narrow the question."
                break

            if event["type"] == "_done":
                final = str(event.get("final", "")).strip()
                session = STORE.get(session_id)
                if final:
                    gate = detect_gate(final)
                    clarification = None
                    if gate:
                        set_stage(session, gate, "waiting")
                        await websocket.send_text(json.dumps(
                            {"type": "stage", "stage": gate, "status": "waiting", "label": stage_label(gate)},
                            ensure_ascii=False,
                        ))
                        if gate == "confirm_problem":
                            clarification = extract_clarification(final)
                            if clarification:
                                session["clarification"] = {**clarification, "status": "draft"}
                    if clarification:
                        assistant_message = ui_message("assistant", final, clarification=clarification)
                    else:
                        assistant_message = ui_message("assistant", final)
                    session["messages"].append(assistant_message)
                    STORE.save(session)
                    await websocket.send_text(
                        json.dumps({"type": "assistant_final", "message": assistant_message,
                                    "stages": session["stages"]}, ensure_ascii=False)
                    )
                else:
                    run_error = "This run produced no reply. Please retry."
                break

            if event["type"] == "_error":
                run_error = "Something went wrong during the run. Please retry later."
                break

            if event["type"] == "stage":
                session = STORE.get(session_id)
                set_stage(session, event["stage"], event.get("status", "running"))
                STORE.save(session)
                await websocket.send_text(json.dumps({
                    "type": "stage",
                    "stage": event["stage"],
                    "status": event.get("status", "running"),
                    "label": stage_label(event["stage"]),
                    "detail": STAGE_RUNNING_TEXT.get(event["stage"], ""),
                    "stages": session["stages"],
                }, ensure_ascii=False))
    except WebSocketDisconnect:
        future.cancel()
        return

    if run_error:
        session = STORE.get(session_id)
        error_message = ui_message("system", run_error, tone="error")
        session["messages"].append(error_message)
        STORE.save(session)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": error_message}, ensure_ascii=False))
        except Exception:
            pass

    try:
        await websocket.send_text(json.dumps({"type": "run_done"}, ensure_ascii=False))
    except Exception:
        pass


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", 8095))
    print(f"\n  Ontology QA Agent frontend\n  ➜ http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
