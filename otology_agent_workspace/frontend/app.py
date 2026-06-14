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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
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
from harness.ontology.data_extractor import (
    persist_extraction,
    schema_outline,
    validate_instances,
)
from harness.ontology.schema_builder import write_draft_schema
from harness.ontology.schema_judge import mechanical_schema_check
from harness.ontology.schema_utils import parse_schema
from harness.ontology.solver import read_solver_result
from harness.ontology.workspace_builder import build_workspace

AGENT_ID = "ontology_coordinator"
UI_BRAND = "Ontology QA Agent"
STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_DIR = ROOT / "outputs" / "ontology_coordinator" / "frontend_sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = ROOT / "otology_agent_workspace" / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
RUNS_DIR = ROOT / "runs" / "ontology_workspace_runs"
ALLOWED_UPLOAD_SUFFIXES = {".csv", ".txt", ".md"}

EXECUTOR = ThreadPoolExecutor(max_workers=4)
_AGENT_LOCK = threading.Lock()
_AGENT_SINGLETON = None
_AGENT_CACHE: dict[str, Any] = {}

# Autonomous mode: the ontology_coordinator LLM drives the entire flow by
# delegating to its six subagents with the `task` tool. There are no human
# confirmation gates — each pipeline stage maps 1:1 to the subagent that owns it.
PIPELINE_STAGES = [
    {"id": "clarify", "label": "Clarify Question"},
    {"id": "evidence", "label": "Collect Evidence"},
    {"id": "schema_build", "label": "Build Schema"},
    {"id": "schema_judge", "label": "Judge Schema"},
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

# Identity of the orchestrator and each subagent, surfaced to the UI so a viewer
# can tell which agent is doing which task. ``__coordinator__`` is a virtual lane
# carrying the main agent's own orchestration reasoning (it is not a stage).
COORDINATOR_LANE = "__coordinator__"

AGENT_LABELS = {
    "ontology_coordinator": "Coordinator",
    "problem_clarifier": "Problem Clarifier",
    "evidence_collector": "Evidence Collector",
    "schema_builder": "Schema Builder",
    "schema_judger": "Schema Judger",
    "data_extractor": "Data Extractor",
    "workspace_solver": "Workspace Solver",
}

STAGE_RUNNING_TEXT = {
    "clarify": "Analyzing and clarifying the question…",
    "evidence": "Gathering evidence (reading uploads, searching the web if needed)…",
    "schema_build": "Building the ontology schema…",
    "schema_judge": "Judging whether the schema can answer the question…",
    "extract": "Extracting data against the confirmed schema…",
    "solve": "Executing code in the workspace to solve the question…",
}

STAGE_ACTIVITY_TEXT = {
    "clarify": "Clarifying the problem statement and solution plan.",
    "evidence": "Collecting uploaded and public evidence as needed.",
    "schema_build": "Building an editable ontology schema from the evidence.",
    "schema_judge": "Checking whether the current schema can answer the question.",
    "extract": "Extracting instances, attributes, and relations with the confirmed schema.",
    "solve": "Solving the question from the extracted workspace data.",
}

TOOL_ACTIVITY_TEXT = {
    "source_reader": "Reading the provided evidence.",
    "evidence_retriever": "Retrieving relevant evidence snippets.",
    "web_search": "Looking up supplemental public evidence.",
    "schema_validator": "Validating schema entities, fields, and relations.",
    "write_todos": "Planning the current processing step.",
    "write_file": "Saving the current step's output.",
    "task": "Running the specialist worker for this step.",
    "execute_code": "Executing answer code and reading the result.",
}


USER_VISIBLE_OUTPUT_CONTRACT = """
User-visible output contract:
- Never reveal local filesystem paths, run directories, schema paths, workspace paths, or generated artifact filenames.
- Do not mention internal tool names or implementation details.
- During schema confirmation, show only:
  1. Entity Definitions: Entity, Entity type, Entity data type.
  2. Relation Schema: Head entity, Head entity type, Head entity data type, Relation name, Tail entity, Tail entity type, Tail entity data type.
- Do not include answerability judgment, evidence source counts, missing requirements, schema paths, draft code, or explanatory paragraphs in the schema-confirmation message.
- Final answers should contain the direct answer only; do not append schema path, workspace path, or source-file summaries.
""".strip()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def redact_paths(text: str) -> str:
    """Remove local filesystem paths from user-visible text."""
    if not isinstance(text, str) or not text:
        return text
    text = re.sub(r"(?<![\w])(?:/[^\s`'\"<>|)]+)+", "[hidden path]", text)
    text = re.sub(r"\b[A-Za-z]:\\[^\s`'\"<>|)]+", "[hidden path]", text)
    return text


def sanitize_user_visible_output(text: str) -> str:
    text = redact_paths(text)
    if not isinstance(text, str) or not text:
        return text
    filtered: list[str] = []
    skip_code_block = False
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if stripped.startswith("```"):
            skip_code_block = not skip_code_block
            continue
        if skip_code_block:
            continue
        if lower.startswith((
            "schema path:",
            "**schema path**:",
            "schema used:",
            "**schema used**:",
            "source files:",
            "**source files**:",
            "workspace path:",
            "**workspace path**:",
        )):
            continue
        filtered.append(line)
    return "\n".join(filtered).strip()


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
            "run_id": f"sess-{session_id}",
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
        if not data.get("run_id"):
            data["run_id"] = f"sess-{data.get('id', path.stem)}"
        return data

    def save(self, session: dict[str, Any]) -> None:
        session["updated_at"] = now_iso()
        write_json(session_path(session["id"]), session)

    def delete(self, session_id: str) -> None:
        path = session_path(session_id)
        if path.exists():
            path.unlink()


STORE = SessionStore()


def safe_session_id(session_id: str) -> str:
    safe = "".join(ch for ch in str(session_id) if ch.isalnum() or ch in "-_")
    if not safe:
        raise HTTPException(status_code=400, detail="Invalid session id")
    return safe


def session_run_id(session: dict[str, Any]) -> str:
    return Path(str(session.get("run_id") or f"sess-{session['id']}")).name


def session_run_dir(session: dict[str, Any]) -> Path:
    return RUNS_DIR / session_run_id(session)


def vrun_for(run_dir: Path) -> str:
    """Virtual (root-relative) run path passed to subagents so write_file /
    execute_code / ontology tools resolve under the harness root."""
    return f"/runs/ontology_workspace_runs/{Path(run_dir).name}"


def run_dir_for_session(session_id: str) -> Path | None:
    """Resolve a session id to its run directory (None if the session is unknown)."""
    if not session_id:
        return None
    try:
        session = STORE.get(session_id)
    except HTTPException:
        return None
    return session_run_dir(session)


def session_upload_dir(session_id: str, create: bool = False) -> Path:
    path = UPLOAD_DIR / safe_session_id(session_id)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


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
        "upload_count": total_upload_count(),
    }


# ─── Uploads (scoped per session) ─────────────────────────────────────────────

def total_upload_count() -> int:
    if not UPLOAD_DIR.exists():
        return 0
    return sum(
        1
        for path in UPLOAD_DIR.rglob("*")
        if path.is_file() and path.suffix.lower() in ALLOWED_UPLOAD_SUFFIXES
    )


def list_uploads(session_id: str) -> list[dict[str, Any]]:
    upload_dir = session_upload_dir(session_id)
    items = []
    for path in sorted(upload_dir.iterdir() if upload_dir.exists() else []):
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


def safe_upload_path(session_id: str, upload_id: str) -> Path:
    upload_dir = session_upload_dir(session_id)
    name = Path(upload_id).name
    path = (upload_dir / name).resolve()
    if upload_dir.resolve() not in path.parents:
        raise HTTPException(status_code=400, detail="Invalid upload id")
    return path


@app.get("/api/uploads")
async def uploads_index(session_id: str = ""):
    if not session_id:
        return {"uploads": []}
    return {"uploads": list_uploads(session_id)}


@app.post("/api/uploads")
async def upload_file(session_id: str = Form(...), file: UploadFile = File(...)):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(status_code=400, detail="Only csv / txt / md files are supported")
    upload_dir = session_upload_dir(session_id, create=True)
    stem = re.sub(r"[^\w.\-]+", "_", Path(file.filename or "upload").stem)[:60] or "upload"
    target = upload_dir / f"{stem}{suffix}"
    counter = 1
    while target.exists():
        target = upload_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    target.write_bytes(await file.read())
    return {"ok": True, "upload": {"id": target.name, "name": target.name}}


@app.delete("/api/uploads/{upload_id}")
async def delete_upload(upload_id: str, session_id: str = ""):
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    path = safe_upload_path(session_id, upload_id)
    if path.exists():
        path.unlink()
    return {"ok": True}


@app.get("/api/uploads/{upload_id}/preview")
async def preview_upload(upload_id: str, session_id: str = ""):
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    path = safe_upload_path(session_id, upload_id)
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


def resolve_artifact_dir(session_id: str, relative: str) -> Path | None:
    """Run dir for a session's artifact (legacy global fallback when no session id)."""
    if session_id:
        return run_dir_for_session(session_id)
    return latest_run_with(relative)


def load_web_evidence_files(run_dir: Path) -> list[dict[str, Any]]:
    """Persisted web evidence written by the web_search tool for this run."""
    web_dir = run_dir / "intermediate" / "web_evidence"
    out: list[dict[str, Any]] = []
    if not web_dir.exists():
        return out
    for path in sorted(web_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            record = read_json(path)
        except Exception:
            continue
        out.append({
            "source_id": str(record.get("source_id", path.stem)),
            "source_kind": "web",
            "file_type": "html",
            "reason": record.get("query", ""),
            "url": record.get("url", ""),
            "title": record.get("title", ""),
            "stage": record.get("collected_stage", "evidence"),
        })
    return out


@app.get("/api/evidence")
async def evidence_summary(session_id: str = ""):
    run_dir = resolve_artifact_dir(session_id, "intermediate/evidence_manifest.json")
    if run_dir is None:
        return {"ok": True, "run_id": "", "question": "", "sources": [], "needs_web_search": False}
    manifest: dict[str, Any] = {}
    manifest_path = run_dir / "intermediate" / "evidence_manifest.json"
    if manifest_path.exists():
        try:
            manifest = read_json(manifest_path)
        except Exception:
            manifest = {}
    sources = []
    seen_web: set[str] = set()
    for source in manifest.get("sources", []) or []:
        if not isinstance(source, dict):
            continue
        kind = source.get("source_kind", "")
        entry = {
            "source_id": Path(str(source.get("source_id", ""))).name,
            "source_kind": kind,
            "file_type": source.get("file_type", ""),
            "reason": source.get("reason", ""),
            "url": source.get("url", ""),
            "title": source.get("title", ""),
            "stage": source.get("collected_stage", ""),
        }
        if kind == "web":
            seen_web.add(entry["url"] or entry["source_id"])
        sources.append(entry)
    # Merge persisted web evidence files not already registered in the manifest so
    # both search rounds remain visible even if the agent did not re-list them.
    for record in load_web_evidence_files(run_dir):
        key = record["url"] or record["source_id"]
        if key in seen_web:
            continue
        seen_web.add(key)
        sources.append(record)
    needs_web = bool(manifest.get("needs_web_search", False)) or any(
        s["source_kind"] == "web" for s in sources
    )
    return {
        "ok": True,
        "run_id": run_dir.name,
        "question": manifest.get("question", ""),
        "sources": sources,
        "needs_web_search": needs_web,
    }


def schema_payload(run_dir: Path) -> dict[str, Any]:
    confirmed = run_dir / "concepts" / "confirmed_schema.py"
    draft = run_dir / "concepts" / "draft_schema.py"
    path = confirmed if confirmed.exists() else draft
    status = "confirmed" if confirmed.exists() else "draft"
    text = path.read_text(encoding="utf-8")
    form = schema_to_form(schema_text=text)
    return {"ok": True, "run_id": run_dir.name, "status": status, "form": form, "schema_text": text}


def csv_cell(value: Any) -> str:
    text = str(value or "")
    if any(ch in text for ch in [",", '"', "\n", "\r"]):
        return '"' + text.replace('"', '""') + '"'
    return text


@app.get("/api/schema")
async def get_schema(session_id: str = ""):
    if session_id:
        run_dir = run_dir_for_session(session_id)
    else:
        run_dir = latest_run_with("concepts/draft_schema.py") or latest_run_with("concepts/confirmed_schema.py")
    has_schema = run_dir is not None and (
        (run_dir / "concepts" / "draft_schema.py").exists()
        or (run_dir / "concepts" / "confirmed_schema.py").exists()
    )
    if not has_schema:
        return {"ok": True, "run_id": run_dir.name if run_dir else "", "status": "none", "form": [], "schema_text": ""}
    try:
        return schema_payload(run_dir)
    except Exception as exc:
        return {"ok": False, "run_id": run_dir.name, "status": "error", "error": str(exc), "form": [], "schema_text": ""}


@app.get("/api/schema/download")
async def download_schema_artifact(session_id: str = "", kind: str = "python"):
    run_dir = run_dir_for_session(session_id) if session_id else (
        latest_run_with("concepts/draft_schema.py") or latest_run_with("concepts/confirmed_schema.py")
    )
    if run_dir is None:
        raise HTTPException(status_code=404, detail="Schema not found")
    payload = schema_payload(run_dir)
    kind = kind.lower().strip()
    if kind == "python":
        filename = "ontology_schema.py"
        return Response(
            content=payload["schema_text"],
            media_type="text/x-python",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    form = payload.get("form") or []
    entities = [item for item in form if item.get("type") == "entity"]
    entity_meta = {item.get("name", ""): item for item in entities}
    if kind == "entities":
        rows = ["Entity,Entity Type,Entity Data Type"]
        rows.extend(
            f'{csv_cell(item.get("name", ""))},{csv_cell(item.get("entity_type", ""))},{csv_cell(item.get("value_type", "str"))}'
            for item in entities
        )
        filename = "entity_definitions.csv"
    elif kind == "relations":
        rows = ["Head Entity,Head Entity Type,Head Entity Data Type,Relation Name,Tail Entity,Tail Entity Type,Tail Entity Data Type"]
        for item in (entry for entry in form if entry.get("type") == "relation"):
            head = entity_meta.get(item.get("head_entity", ""), {})
            tail = entity_meta.get(item.get("tail_entity", ""), {})
            rows.append(",".join([
                csv_cell(item.get("head_entity", "")),
                csv_cell(head.get("entity_type", "")),
                csv_cell(head.get("value_type", "str")),
                csv_cell(item.get("relation", "")),
                csv_cell(item.get("tail_entity", "")),
                csv_cell(tail.get("entity_type", "")),
                csv_cell(tail.get("value_type", "str")),
            ]))
        filename = "relation_schema.csv"
    else:
        raise HTTPException(status_code=400, detail="Unknown schema artifact kind")
    return Response(
        content="\n".join(rows) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
async def results_summary(session_id: str = ""):
    run_dir = resolve_artifact_dir(session_id, "intermediate/extraction_report.json")
    report: dict[str, Any] = {}
    answer_sources: list[str] = []
    report_path = run_dir / "intermediate" / "extraction_report.json" if run_dir is not None else None
    if report_path is not None and report_path.exists():
        try:
            raw = read_json(report_path)
            report = {
                "total_instances": raw.get("total_instances"),
                "total_facts": raw.get("total_facts"),
                "total_relations": raw.get("total_relations"),
                "avg_confidence": raw.get("avg_confidence"),
                "relation_types_used": raw.get("relation_types_used", []),
            }
        except Exception:
            report = {}
    if run_dir is not None:
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
    message = {"id": uuid4().hex[:10], "role": role, "content": sanitize_user_visible_output(content), "timestamp": now_iso()}
    message.update(extra)
    return message


def compact_model_output(text: str, max_chars: int = 1600) -> str:
    cleaned = sanitize_user_visible_output(text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "\n…"


def model_activity(stage_id: str, output: str) -> dict[str, Any]:
    title = stage_label(stage_id)
    cleaned = compact_model_output(output)
    return ui_message(
        "event",
        "Model returned a structured update.",
        kind="stage",
        title=title,
        stage=stage_id,
        status="running",
        thinking="Reviewing the latest structured model result for this step.",
        output=cleaned,
    )


def stage_activity(stage_id: str, status: str = "running") -> dict[str, Any]:
    title = stage_label(stage_id)
    content = STAGE_ACTIVITY_TEXT.get(stage_id, STAGE_RUNNING_TEXT.get(stage_id, title))
    if status == "done":
        content = f"{title} is complete."
    return ui_message("event", content, kind="stage", title=title, stage=stage_id, status=status)


def tool_activity(tool_name: str, status: str = "running", call_id: str = "", output: str = "") -> dict[str, Any]:
    # Only a fixed, tool-name-bound sentence is surfaced; raw tool output and
    # arguments are intentionally never sent to the client.
    content = TOOL_ACTIVITY_TEXT.get(tool_name, "Running an internal processing step.")
    extra: dict[str, Any] = {"tool": tool_name}
    if call_id:
        extra["tool_call_id"] = call_id
    return ui_message("event", content, kind="tool", title="Tool activity", status=status, **extra)


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


def get_cached_agent(agent_id: str = AGENT_ID):
    global _AGENT_SINGLETON
    if agent_id == AGENT_ID and _AGENT_SINGLETON is not None:
        return _AGENT_SINGLETON
    if agent_id in _AGENT_CACHE:
        return _AGENT_CACHE[agent_id]
    with _AGENT_LOCK:
        if agent_id == AGENT_ID and _AGENT_SINGLETON is not None:
            return _AGENT_SINGLETON
        if agent_id in _AGENT_CACHE:
            return _AGENT_CACHE[agent_id]
        from harness.agents.agent_loop import build_agent
        from harness.agents.registry import AgentRegistry
        from harness.config import load_config

        cfg = load_config(str(ROOT / "harness.json"))
        registry = AgentRegistry(cfg)
        agent_cfg = registry.get(agent_id)
        cached = (build_agent(agent_cfg, ".", registry=registry), agent_cfg)
        _AGENT_CACHE[agent_id] = cached
        if agent_id == AGENT_ID:
            _AGENT_SINGLETON = cached
        return cached


def get_cached_coordinator_agent():
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


def _autonomous_message(
    question: str, upload_paths: list[str], workspace_dir: str, run_id: str
) -> str:
    """Build the single user message that puts the coordinator in autonomous mode.

    Mirrors ``harness/ontology/pipeline.py``: the coordinator drives the entire
    workflow itself by delegating to its subagents with the ``task`` tool, with
    no human confirmation gates."""
    inputs = {
        "question": question,
        "upload_paths": upload_paths,
        "workspace_dir": workspace_dir,
        "run_id": run_id,
        "autonomous": True,
    }
    return (
        "You are running in autonomous backend mode: no human is available to "
        "confirm the gates, so you must drive the COMPLETE ontology workflow end "
        "to end yourself by delegating to your subagents with the `task` tool, "
        "and only then give the final answer.\n\n"
        "Follow your Required Workflow in order. Do not pause to ask the user to "
        "confirm the clarified problem or the schema; proceed automatically through "
        "every step. Make sure `workspace_solver` has written "
        f"`{workspace_dir}/intermediate/solver_result.json` before you produce the "
        "final answer.\n\nInputs:\n"
        + json.dumps(inputs, ensure_ascii=False, indent=2)
        + f"\n\n{USER_VISIBLE_OUTPUT_CONTRACT}"
    )


def run_coordinator_autonomous(
    question: str, upload_paths: list[str], thread_id: str, run_dir: Path, emit
) -> str:
    """Drive one question with the pure-LLM ontology_coordinator.

    The coordinator LLM owns the whole flow: it delegates to each subagent with
    the ``task`` tool, and the backend only streams what happens. There is no
    Python state machine, no hardcoded step routing, no fallback. The UI events
    carry an explicit coordinator-vs-subagent identity so a viewer can see which
    agent is doing which task:

    - the coordinator's own reasoning streams on the virtual ``__coordinator__``
      lane (it is the orchestrator, not a stage);
    - each ``task`` delegation flips the corresponding stage to ``running`` and
      tags the stage event with the responsible subagent;
    - a subagent's own model output / tool calls stream on that subagent's stage.
    """
    from langchain_core.messages import HumanMessage

    agent, _agent_cfg = get_cached_coordinator_agent()
    # Isolate this question in its own run directory (evidence, schema, data) so
    # the coordinator and its subagents share one workspace (ontology tools key
    # off HARNESS_RUN_DIR), and create the canonical run subdirectories up front.
    run_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("concepts", "data", "src", "intermediate", "intermediate/web_evidence"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    os.environ["HARNESS_ROOT"] = str(ROOT)
    os.environ["HARNESS_AGENT_ID"] = AGENT_ID
    os.environ["HARNESS_RUN_DIR"] = str(run_dir.resolve())

    run_id = run_dir.name
    workspace_dir = vrun_for(run_dir)
    message = _autonomous_message(question, upload_paths, workspace_dir, run_id)

    final_content = ""
    current_stage = ""
    seen_task_calls: set[str] = set()
    seen_tool_calls: set[str] = set()
    done_tool_calls: set[str] = set()
    emitted_outputs: set[str] = set()

    # Live token streaming, keyed by lane. The coordinator's reasoning goes to the
    # COORDINATOR_LANE; a subagent's reasoning goes to its stage lane.
    thinking_buf = ""
    output_buf = ""
    last_step_key: str | None = None
    last_emit_at = 0.0
    last_emit_len = 0
    stream_lane = COORDINATOR_LANE

    def push_stream(lane: str, force: bool = False) -> None:
        nonlocal last_emit_at, last_emit_len
        now = time.monotonic()
        size = len(thinking_buf) + len(output_buf)
        if not force and size - last_emit_len < 48 and now - last_emit_at < 0.35:
            return
        last_emit_at = now
        last_emit_len = size
        emit({"type": "stream", "stage": lane, "thinking": thinking_buf, "output": output_buf})

    config = {
        "configurable": {"thread_id": thread_id or f"{run_id}:{AGENT_ID}"},
        "recursion_limit": 300,
    }
    for item in agent.stream(
        {"messages": [HumanMessage(content=message)]},
        config=config,
        stream_mode=["messages", "values"],
        subgraphs=True,
    ):
        if not isinstance(item, tuple) or len(item) != 3:
            continue
        namespace, mode, data = item

        if mode == "messages":
            chunk, meta = data
            if meta.get("langgraph_node") != "model":
                continue
            # namespace == () is the coordinator; anything deeper is the subagent
            # currently delegated to (tracked from the last `task` call).
            lane = COORDINATOR_LANE if namespace == () else (current_stage or COORDINATOR_LANE)
            step_key = f"{repr(namespace)}|{meta.get('langgraph_step')}|{meta.get('run_id')}"
            if step_key != last_step_key:
                last_step_key = step_key
                thinking_buf = ""
                output_buf = ""
                last_emit_len = 0
                stream_lane = lane
            thinking_buf, output_buf = _accumulate_stream(chunk, thinking_buf, output_buf)
            push_stream(stream_lane)
            continue

        if mode != "values":
            continue
        messages = data.get("messages", []) if isinstance(data, dict) else []
        if not messages:
            continue
        last = messages[-1]
        if getattr(last, "type", "") in ("ai", "tool"):
            push_stream(stream_lane, force=True)

        for msg in messages:
            mtype = getattr(msg, "type", "")
            if mtype == "ai":
                for idx, call in enumerate(getattr(msg, "tool_calls", None) or []):
                    cid = str(call.get("id") or f"{call.get('name', '')}:{idx}")
                    tool_name = str(call.get("name", ""))
                    if tool_name == "task":
                        if cid in seen_task_calls:
                            continue
                        seen_task_calls.add(cid)
                        args = call.get("args", {}) or {}
                        subagent = str(args.get("subagent_type", "") or args.get("agent", ""))
                        stage = SUBAGENT_STAGE.get(subagent, "")
                        if stage:
                            current_stage = stage
                            emit({
                                "type": "stage",
                                "stage": stage,
                                "status": "running",
                                "agent": subagent,
                                "agent_label": AGENT_LABELS.get(subagent, subagent),
                            })
                    else:
                        if cid in seen_tool_calls:
                            continue
                        seen_tool_calls.add(cid)
                        if tool_name:
                            emit({"type": "activity", "message": tool_activity(tool_name, status="running", call_id=cid)})
            elif mtype == "tool":
                cid = str(getattr(msg, "tool_call_id", "") or "")
                if not cid or cid in done_tool_calls:
                    continue
                done_tool_calls.add(cid)
                tool_name = str(getattr(msg, "name", "") or "")
                if tool_name and tool_name != "task":
                    result = getattr(msg, "content", "")
                    if isinstance(result, list):
                        result = " ".join(str(part) for part in result)
                    emit({"type": "activity", "message": tool_activity(tool_name, status="done", call_id=cid, output=str(result))})

        last_type = getattr(last, "type", "")
        last_calls = getattr(last, "tool_calls", None) or []
        content = getattr(last, "content", "") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if last_type == "ai" and content and not last_calls:
            if namespace == ():
                # The coordinator's final, tool-call-free message is the answer.
                final_content = content
            elif current_stage:
                # A subagent finished its step: persist a compact output card so
                # the run history shows what that agent produced.
                if content not in emitted_outputs:
                    emitted_outputs.add(content)
                    emit({"type": "activity", "message": model_activity(current_stage, content)})

    if current_stage:
        emit({"type": "stage", "stage": current_stage, "status": "done"})
    return final_content


def _accumulate_stream(chunk: Any, thinking_buf: str, output_buf: str) -> tuple[str, str]:
    """Fold one streamed model chunk into the live thinking/output buffers.

    DeepSeek thinking-mode streams incremental ``reasoning_content`` deltas on
    ``additional_kwargs`` (append) and the answer text on ``chunk.content``. The
    latter may arrive either as incremental deltas or as a cumulative snapshot,
    so we detect which and either append or replace to avoid duplication.
    """
    kwargs = getattr(chunk, "additional_kwargs", None) or {}
    if isinstance(kwargs, dict):
        reasoning_inc = kwargs.get("reasoning_content")
        if isinstance(reasoning_inc, str) and reasoning_inc:
            thinking_buf += reasoning_inc
        content_kw = kwargs.get("content")
        if isinstance(content_kw, str) and content_kw:
            output_buf += content_kw
            return thinking_buf, output_buf
    text = getattr(chunk, "content", "")
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if isinstance(text, str) and text:
        if output_buf and text.startswith(output_buf):
            output_buf = text  # cumulative snapshot
        else:
            output_buf += text  # incremental delta
    return thinking_buf, output_buf


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
            elif payload.get("type") == "history":
                await websocket.send_text(
                    json.dumps({"type": "history", "session": STORE.get(session["id"])}, ensure_ascii=False)
                )
    except WebSocketDisconnect:
        return


async def handle_chat(websocket: Any, session_id: str, content: str, upload_ids: list[str]) -> None:
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
    activity_seen: set[str] = set()
    start_activity = ui_message(
        "event",
        "Question received. The ontology QA pipeline is starting.",
        kind="run_start",
        title="Start processing",
        status="running",
    )
    session["messages"].append(start_activity)
    STORE.save(session)
    await websocket.send_text(json.dumps({"type": "activity", "message": start_activity}, ensure_ascii=False))

    agent_input = content
    paths: list[str] = []
    if upload_names:
        upload_root = Path("otology_agent_workspace/data/uploads") / safe_session_id(session_id)
        paths = [str(upload_root / name) for name in upload_names]
        session["upload_paths"] = paths
        STORE.save(session)
        agent_input = f"{content}\n\nupload_paths: {json.dumps(paths, ensure_ascii=False)}"
    else:
        stored_paths = session.get("upload_paths", [])
        if isinstance(stored_paths, list):
            paths = [str(item) for item in stored_paths if str(item).strip()]

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit(event: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def run_agent_thread() -> None:
        try:
            # Each question gets a fresh run directory so the coordinator and its
            # subagents always write into a clean workspace (write_file refuses to
            # overwrite) and the Workbench reads the latest run. Persist the run_id
            # before invoking so the artifact endpoints resolve to this run.
            run_id = f"sess-{safe_session_id(session_id)}-{int(time.time() * 1000)}"
            current = STORE.get(session_id)
            current["run_id"] = run_id
            STORE.save(current)
            final = run_coordinator_autonomous(
                content, paths, f"{run_id}:{AGENT_ID}", RUNS_DIR / run_id, emit
            )
            emit({"type": "_done", "final": final})
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a friendly error.
            import traceback
            traceback.print_exc()
            sys.stderr.flush()
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
                # Autonomous mode: the coordinator's final message is the answer.
                # There are no confirmation gates to infer.
                final = str(event.get("final", "")).strip()
                session = STORE.get(session_id)
                if final:
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

            if event["type"] == "stream":
                # Live token stream for the active stage. Ephemeral: forwarded to
                # the client for the live card but not persisted to history.
                await websocket.send_text(json.dumps({
                    "type": "stream",
                    "stage": event.get("stage", ""),
                    "thinking": redact_paths(event.get("thinking", "")),
                    "output": redact_paths(event.get("output", "")),
                }, ensure_ascii=False))
                continue

            if event["type"] == "activity":
                session = STORE.get(session_id)
                message = event.get("message")
                if isinstance(message, dict):
                    session["messages"].append(message)
                    STORE.save(session)
                    await websocket.send_text(json.dumps({"type": "activity", "message": message}, ensure_ascii=False))
                continue

            if event["type"] == "stage":
                session = STORE.get(session_id)
                set_stage(session, event["stage"], event.get("status", "running"))
                status = event.get("status", "running")
                key = f"{event['stage']}:{status}"
                activity = None
                if status in ("running", "waiting", "done") and key not in activity_seen:
                    activity_seen.add(key)
                    activity = stage_activity(event["stage"], status)
                    session["messages"].append(activity)
                STORE.save(session)
                await websocket.send_text(json.dumps({
                    "type": "stage",
                    "stage": event["stage"],
                    "status": status,
                    "label": stage_label(event["stage"]),
                    "detail": STAGE_RUNNING_TEXT.get(event["stage"], ""),
                    "agent": event.get("agent", ""),
                    "agent_label": event.get("agent_label", ""),
                    "stages": session["stages"],
                }, ensure_ascii=False))
                if activity is not None:
                    await websocket.send_text(json.dumps({"type": "activity", "message": activity}, ensure_ascii=False))
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


class HttpEventSink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_text(self, text: str) -> None:
        self.events.append(json.loads(text))


@app.post("/api/chat/{session_id}")
async def chat_fallback(session_id: str, request: Request):
    payload = await request.json()
    try:
        session = STORE.get(session_id)
    except HTTPException:
        session = STORE.create()

    sink = HttpEventSink()
    payload_type = payload.get("type")
    if payload_type == "chat":
        await handle_chat(
            sink,
            session["id"],
            str(payload.get("content", "")),
            list(payload.get("upload_ids", []) or []),
        )
    elif payload_type == "history":
        sink.events.append({"type": "history", "session": STORE.get(session["id"])})
    else:
        raise HTTPException(status_code=400, detail="Unsupported chat payload type")

    return {"ok": True, "session_id": session["id"], "events": sink.events}


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", 8095))
    print(f"\n  Ontology QA Agent frontend\n  ➜ http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
