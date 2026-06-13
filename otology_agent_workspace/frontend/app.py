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
from harness.ontology.data_extractor import extract_company_csv
from harness.ontology.schema_builder import build_draft_schema
from harness.ontology.schema_judge import judge_schema
from harness.ontology.schema_utils import parse_schema
from harness.ontology.solver import solve_company_workspace
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
MOCK_MODE = os.environ.get("ONTOLOGY_UI_MOCK", "") == "1"

EXECUTOR = ThreadPoolExecutor(max_workers=4)
_AGENT_LOCK = threading.Lock()
_AGENT_SINGLETON = None
_AGENT_CACHE: dict[str, Any] = {}

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

STAGE_ACTIVITY_TEXT = {
    "clarify": "Clarifying the problem statement and solution plan.",
    "confirm_problem": "Problem clarification is ready for your confirmation.",
    "evidence": "Collecting uploaded and public evidence as needed.",
    "schema_build": "Building an editable ontology schema from the evidence.",
    "schema_judge": "Checking whether the current schema can answer the question.",
    "confirm_schema": "Schema is ready and waiting for your confirmation.",
    "extract": "Extracting instances, attributes, and relations with the confirmed schema.",
    "solve": "Solving the question from the extracted workspace data.",
}

TOOL_ACTIVITY_TEXT = {
    "source_reader": "Reading the provided evidence.",
    "evidence_retriever": "Retrieving relevant evidence snippets.",
    "web_search": "Looking up supplemental public evidence.",
    "schema_validator": "Validating schema entities, fields, and relations.",
    "schema_draft_builder": "Preparing the draft schema.",
    "evidence_manifest_writer": "Saving the evidence manifest.",
    "write_todos": "Planning the current processing step.",
    "task": "Running the specialist worker for this step.",
    "data_extract_company_csv": "Extracting structured records from tabular evidence.",
    "workspace_builder_tool": "Preparing the executable answer workspace.",
    "workspace_solver_tool": "Running the answer workflow.",
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
        "mock": MOCK_MODE,
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


def tool_activity(tool_name: str) -> dict[str, Any]:
    content = TOOL_ACTIVITY_TEXT.get(tool_name, "Running an internal processing step.")
    return ui_message("event", content, kind="tool", title="Tool activity", status="running")


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


def reset_stages_after(session: dict[str, Any], stage_id: str) -> None:
    order = [s["id"] for s in PIPELINE_STAGES]
    if stage_id not in order:
        return
    target_index = order.index(stage_id)
    for stage in session["stages"]:
        if order.index(stage["id"]) > target_index:
            stage["status"] = "pending"


def get_stage_status(session: dict[str, Any], stage_id: str) -> str:
    for stage in session.get("stages", []):
        if stage.get("id") == stage_id:
            return str(stage.get("status", "pending"))
    return "pending"


def infer_waiting_gate(session: dict[str, Any], final: str, clarification: dict[str, Any] | None) -> str:
    if clarification:
        return "confirm_problem"
    problem_status = get_stage_status(session, "confirm_problem")
    evidence_status = get_stage_status(session, "evidence")
    if problem_status != "done" and evidence_status == "pending":
        return "confirm_problem"
    return detect_gate(final)


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
        match = re.match(r"^\*{0,2}(?:Question|\u95ee\u9898|\u6838\u5fc3\u95ee\u9898)\s*[:\uff1a]\*{0,2}\s*(.+)$", stripped)
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
    if re.search(r"(draft\s+schema|schema\s+is\s+ready|schema\s+ready|answerability\s+judgment|relation\s+table|confirm\s+.{0,24}schema)", lowered):
        return "confirm_schema"
    if re.search(r"(草案\s*schema|schema\s*(已准备|准备好|可确认)|确认.{0,12}(schema|模式|本\s*schema|当前\s*schema|草案))", text, flags=re.IGNORECASE):
        return "confirm_schema"
    if re.search(r"(问题澄清|核心问题|clarified\s+problem|understanding\s+of\s+the\s+question|please\s+confirm|confirm\s+the\s+clarified)", lowered):
        return "confirm_problem"
    return ""


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


def run_real_agent(message: str, thread_id: str, run_dir: Path, emit) -> str:
    from langchain_core.messages import HumanMessage

    agent, agent_cfg = get_cached_agent()
    # Bind this session to its own run directory so evidence, schema and data
    # artifacts are isolated per session and the two web-search rounds share one
    # web_evidence store (ontology tools key off HARNESS_RUN_DIR).
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HARNESS_ROOT"] = str(ROOT)
    os.environ["HARNESS_AGENT_ID"] = AGENT_ID
    os.environ["HARNESS_RUN_DIR"] = str(run_dir.resolve())
    run_id = run_dir.name
    message = (
        f"{message}\n\n[run_context] run_id={run_id}. Persist every run artifact under "
        f"runs/ontology_workspace_runs/{run_id}/ (evidence_manifest.json, web_evidence/, "
        "draft_schema.py, data/). Reuse web evidence already persisted in this run instead "
        f"of repeating searches.\n\n{USER_VISIBLE_OUTPUT_CONTRACT}"
    )

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
            tool_name = str(call.get("name", ""))
            if tool_name != "task":
                emit({"type": "activity", "message": tool_activity(tool_name)})
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


def run_problem_clarifier_agent(question: str, upload_paths: list[str], thread_id: str, run_dir: Path, emit) -> str:
    from langchain_core.messages import HumanMessage

    agent, _agent_cfg = get_cached_agent("problem_clarifier")
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HARNESS_ROOT"] = str(ROOT)
    os.environ["HARNESS_AGENT_ID"] = "problem_clarifier"
    os.environ["HARNESS_RUN_DIR"] = str(run_dir.resolve())
    payload = json.dumps({
        "question": question,
        "upload_paths": upload_paths,
        "_ui_output_contract": USER_VISIBLE_OUTPUT_CONTRACT,
    }, ensure_ascii=False)
    emit({"type": "stage", "stage": "clarify", "status": "running"})

    final_content = ""
    config = {"configurable": {"thread_id": f"{thread_id}:clarify" or "clarify"}}
    for item in agent.stream(
        {"messages": [HumanMessage(content=payload)]},
        config=config,
        stream_mode=["values"],
        subgraphs=True,
    ):
        if not isinstance(item, tuple) or len(item) != 3:
            continue
        namespace, _mode, data = item
        if namespace != ():
            continue
        messages = data.get("messages", []) if isinstance(data, dict) else []
        if not messages:
            continue
        last = messages[-1]
        if getattr(last, "type", "") != "ai":
            continue
        content = getattr(last, "content", "") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if content:
            final_content = content
            emit({"type": "activity", "message": model_activity("clarify", content)})
    return final_content


def extract_json_payload(text: str) -> dict[str, Any]:
    try:
        from harness.ontology.json_contract import extract_json_object

        data = extract_json_object(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def run_subagent_json(
    agent_id: str,
    payload: dict[str, Any],
    thread_id: str,
    run_dir: Path,
    emit,
    max_tool_calls: int = 18,
    stage_id: str = "",
) -> dict[str, Any]:
    from langchain_core.messages import HumanMessage

    agent, _agent_cfg = get_cached_agent(agent_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ["HARNESS_ROOT"] = str(ROOT)
    os.environ["HARNESS_AGENT_ID"] = agent_id
    os.environ["HARNESS_RUN_DIR"] = str(run_dir.resolve())

    final_content = ""
    emitted_model_output = ""
    emitted_tools: set[str] = set()
    tool_call_count = 0
    config = {"configurable": {"thread_id": f"{thread_id}:{agent_id}"}}
    for item in agent.stream(
        {"messages": [HumanMessage(content=json.dumps({
            **payload,
            "_ui_output_contract": USER_VISIBLE_OUTPUT_CONTRACT,
        }, ensure_ascii=False))]},
        config=config,
        stream_mode=["values"],
        subgraphs=True,
    ):
        if not isinstance(item, tuple) or len(item) != 3:
            continue
        _namespace, _mode, data = item
        messages = data.get("messages", []) if isinstance(data, dict) else []
        if not messages:
            continue
        last = messages[-1]
        for call in getattr(last, "tool_calls", None) or []:
            tool_name = str(call.get("name", ""))
            tool_call_count += 1
            if tool_name not in emitted_tools:
                emitted_tools.add(tool_name)
                emit({"type": "activity", "message": tool_activity(tool_name)})
            if tool_call_count >= max_tool_calls:
                parsed = extract_json_payload(final_content)
                parsed["_raw"] = final_content
                parsed["_truncated"] = True
                return parsed
        if getattr(last, "type", "") == "ai":
            content = getattr(last, "content", "") or ""
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if content and not (getattr(last, "tool_calls", None) or []):
                final_content = content
                if stage_id and content != emitted_model_output:
                    emitted_model_output = content
                    emit({"type": "activity", "message": model_activity(stage_id, content)})
    parsed = extract_json_payload(final_content)
    parsed["_raw"] = final_content
    return parsed


def web_sources_from_run(run_dir: Path) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for item in load_web_evidence_files(run_dir):
        sources.append({
            "source_id": item.get("source_id", ""),
            "source_kind": "web",
            "url": item.get("url", ""),
            "title": item.get("title", ""),
            "evidence_path": str(run_dir / "intermediate" / "web_evidence" / f"{item.get('source_id', '')}.json"),
            "reason": item.get("snippet", "")[:220],
        })
    return [item for item in sources if item["source_id"]]


def merge_sources(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for source in group:
            source_id = str(source.get("source_id", "")).strip()
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            merged.append(source)
    return merged


def schema_plan_for(question: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_id = sources[0].get("source_id", "web_001") if sources else "user_question"
    return [
        {"kind": "entity", "name": "Company", "source_id": source_id, "fields": ["name", "country"]},
        {"kind": "entity", "name": "Industry", "source_id": source_id, "fields": ["name"]},
        {
            "kind": "relation",
            "name": "operates_in_industry",
            "head": "Company",
            "tail": "Industry",
            "source_id": source_id,
        },
    ]


def write_evidence_manifest(run_dir: Path, question: str, payload: dict[str, Any], upload_paths: list[str]) -> Path:
    manifest_path = run_dir / "intermediate" / "evidence_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload_sources = payload.get("sources", [])
    if not isinstance(payload_sources, list):
        payload_sources = []
    upload_sources = [
        {
            "source_id": Path(path).name,
            "source_kind": "upload",
            "file_type": Path(path).suffix.lstrip(".") or "unknown",
            "file_path": path,
            "reason": "User-uploaded evidence for schema building and data extraction.",
        }
        for path in upload_paths
    ]
    sources = merge_sources(payload_sources, upload_sources, web_sources_from_run(run_dir))
    needs_web_search = bool(payload.get("needs_web_search")) or any(
        source.get("source_kind") == "web" for source in sources
    ) or not upload_paths
    schema_plan = payload.get("schema_plan")
    if not isinstance(schema_plan, list) or not schema_plan:
        schema_plan = schema_plan_for(question, sources)
    manifest = {
        "question": question,
        "sources": sources,
        "needs_web_search": needs_web_search,
        "handler": "schema_builder",
        "schema_plan": schema_plan,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def schema_relation_table(schema_text: str) -> str:
    parsed = parse_schema(schema_text)
    entity_meta = {
        class_info.name: (class_info.entity_type, class_info.value_type)
        for class_info in parsed.classes
    }
    rows = []
    for class_info in parsed.classes:
        for field in class_info.fields:
            if field.kind == "relation" and not field.reverse:
                head_type, head_data_type = entity_meta.get(class_info.name, ("", ""))
                tail_type, tail_data_type = entity_meta.get(field.target or "", ("", ""))
                rows.append((
                    class_info.name,
                    head_type,
                    head_data_type,
                    field.name,
                    field.target or "",
                    tail_type,
                    tail_data_type,
                ))
    if not rows:
        return ""
    lines = [
        "| Head entity | Head entity type | Head entity data type | Relation name | Tail entity | Tail entity type | Tail entity data type |",
        "|---|---|---|---|---|---|---|",
    ]
    lines.extend(
        f"| {head} | {head_type} | {head_data_type} | {relation} | {tail} | {tail_type} | {tail_data_type} |"
        for head, head_type, head_data_type, relation, tail, tail_type, tail_data_type in rows
    )
    return "\n".join(lines)


def schema_entity_table(schema_text: str) -> str:
    parsed = parse_schema(schema_text)
    rows = [
        (class_info.name, class_info.entity_type, class_info.value_type)
        for class_info in parsed.classes
    ]
    if not rows:
        return ""
    lines = [
        "| Entity | Entity type | Entity data type |",
        "|---|---|---|",
    ]
    lines.extend(
        f"| {entity} | {entity_type} | {data_type} |"
        for entity, entity_type, data_type in rows
    )
    return "\n".join(lines)


def schema_confirmation_message(run_dir: Path, judgment: dict[str, Any], evidence: dict[str, Any]) -> str:
    draft_path = run_dir / "concepts" / "draft_schema.py"
    schema_text = draft_path.read_text(encoding="utf-8")
    entity_table = schema_entity_table(schema_text)
    relation_table = schema_relation_table(schema_text)
    parts = []
    if entity_table:
        parts.extend(["**Entity Definitions**", "", entity_table])
    if relation_table:
        parts.extend(["", "**Relation Schema**", "", relation_table])
    return "\n".join(parts)


def run_schema_pipeline(question: str, upload_paths: list[str], session: dict[str, Any], emit) -> str:
    run_dir = session_run_dir(session)
    run_dir.mkdir(parents=True, exist_ok=True)

    emit({"type": "stage", "stage": "evidence", "status": "running"})
    evidence_payload = run_subagent_json(
        "evidence_collector",
        {"question": question, "upload_paths": upload_paths},
        session["thread_id"],
        run_dir,
        emit,
        stage_id="evidence",
    )
    manifest_path = write_evidence_manifest(run_dir, question, evidence_payload, upload_paths)
    evidence = read_json(manifest_path)

    emit({"type": "stage", "stage": "schema_build", "status": "running"})
    builder_payload = run_subagent_json(
        "schema_builder",
        {
            "question": question,
            "sources": evidence.get("sources", []),
            "evidence_manifest_path": str(manifest_path),
        },
        session["thread_id"],
        run_dir,
        emit,
        stage_id="schema_build",
    )
    draft_path = run_dir / "concepts" / "draft_schema.py"
    if not draft_path.exists():
        build_draft_schema(question, manifest_path, draft_path)
    if not draft_path.exists() and builder_payload.get("schema_path"):
        source = Path(str(builder_payload["schema_path"]))
        if source.exists():
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    emit({"type": "stage", "stage": "schema_judge", "status": "running"})
    judger_payload = run_subagent_json(
        "schema_judger",
        {"question": question, "schema_path": str(draft_path)},
        session["thread_id"],
        run_dir,
        emit,
        stage_id="schema_judge",
    )
    judgment = judger_payload if "answerable" in judger_payload else judge_schema(question, schema_path=draft_path)
    if not judgment.get("answerable", False):
        deterministic_judgment = judge_schema(question, schema_path=draft_path)
        if deterministic_judgment.get("answerable", False):
            judgment = deterministic_judgment

    emit({"type": "stage", "stage": "confirm_schema", "status": "waiting"})
    return schema_confirmation_message(run_dir, judgment, evidence)


def choose_csv_source(run_dir: Path, upload_paths: list[str]) -> Path:
    for item in upload_paths:
        path = Path(item)
        if path.suffix.lower() == ".csv" and path.exists():
            return path
    return ROOT / "test_data" / "ontology" / "company_sample.csv"


def run_solve_pipeline(question: str, upload_paths: list[str], session: dict[str, Any], emit) -> str:
    run_dir = session_run_dir(session)
    draft_path = run_dir / "concepts" / "draft_schema.py"
    confirmed_path = run_dir / "concepts" / "confirmed_schema.py"
    if draft_path.exists() and not confirmed_path.exists():
        result = confirm_schema(draft_path, confirmed_path)
        if not result.get("valid", False):
            return "Schema confirmation failed; please edit the schema and try again."
    manifest_path = run_dir / "intermediate" / "evidence_manifest.json"
    evidence = read_json(manifest_path) if manifest_path.exists() else {"sources": []}

    emit({"type": "stage", "stage": "extract", "status": "running"})
    run_subagent_json(
        "data_extractor",
        {
            "schema_path": str(confirmed_path),
            "sources": evidence.get("sources", []),
            "evidence_manifest_path": str(manifest_path),
        },
        session["thread_id"],
        run_dir,
        emit,
        stage_id="extract",
    )
    if not (run_dir / "intermediate" / "extraction_report.json").exists():
        extract_company_csv(confirmed_path, choose_csv_source(run_dir, upload_paths), run_dir)

    emit({"type": "stage", "stage": "solve", "status": "running"})
    workspace = build_workspace(
        run_dir,
        confirmed_path,
        run_dir / "data" / "instances.json",
        run_dir / "data" / "facts.csv",
        run_dir / "data" / "relations.csv",
    )
    solver_payload = run_subagent_json(
        "workspace_solver",
        {"question": question, "schema_path": str(confirmed_path), "workspace_dir": str(run_dir)},
        session["thread_id"],
        run_dir,
        emit,
        stage_id="solve",
    )
    solver_path = run_dir / "intermediate" / "solver_result.json"
    solver = read_json(solver_path) if solver_path.exists() else {}
    if not solver.get("ok"):
        solver = solve_company_workspace(question, run_dir)

    emit({"type": "stage", "stage": "solve", "status": "done"})
    if solver_payload.get("_raw") and solver.get("ok"):
        return sanitize_user_visible_output(str(solver_payload["_raw"]).strip())
    if solver.get("ok"):
        return sanitize_user_visible_output(str(solver.get("answer", "")))
    return "Solving failed. Please review the run results and try again."


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


def _write_mock_web_evidence(run_dir: Path, source_id: str, stage: str, url: str, title: str, query: str) -> None:
    web_dir = run_dir / "intermediate" / "web_evidence"
    web_dir.mkdir(parents=True, exist_ok=True)
    (web_dir / f"{source_id}.json").write_text(json.dumps({
        "source_id": source_id,
        "query": query,
        "url": url,
        "title": title,
        "snippet": "Mock persisted web evidence for local UI testing.",
        "retrieved_at": now_iso(),
        "collected_stage": stage,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def run_mock_agent(message: str, session: dict[str, Any], emit) -> str:
    """Deterministic walkthrough of the pipeline for local UI testing."""
    stages = {s["id"]: s["status"] for s in session["stages"]}
    run_dir = session_run_dir(session)
    if stages.get("confirm_problem") == "waiting":
        (run_dir / "concepts").mkdir(parents=True, exist_ok=True)
        (run_dir / "intermediate").mkdir(parents=True, exist_ok=True)
        (run_dir / "concepts" / "draft_schema.py").write_text(MOCK_SCHEMA, encoding="utf-8")
        (run_dir / "intermediate" / "evidence_manifest.json").write_text(json.dumps({
            "question": "Which companies in the US do data analytics?",
            "needs_web_search": True,
            "sources": [
                {"source_id": "company_sample.csv", "source_kind": "upload", "file_type": "csv",
                 "reason": "Sample company table with company name, country and industry fields"},
                {"source_id": "web_001", "source_kind": "web", "file_type": "html",
                 "title": "Top data analytics companies in the US",
                 "url": "https://example.com/us-data-analytics-companies",
                 "collected_stage": "evidence",
                 "reason": "Supplements industry sub-domain labels missing from the upload"},
            ],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        # Round 1 web evidence, persisted to this session's run dir.
        _write_mock_web_evidence(
            run_dir, "web_001", "evidence",
            "https://example.com/us-data-analytics-companies",
            "Top data analytics companies in the US",
            "US data analytics companies",
        )
        for stage, pause in (("evidence", 1.2), ("schema_build", 1.6), ("schema_judge", 1.2)):
            emit({"type": "stage", "stage": stage, "status": "running"})
            time.sleep(pause)
        emit({"type": "stage", "stage": "confirm_schema", "status": "waiting"})
        return schema_confirmation_message(run_dir, {"answerable": True}, {"sources": []})
    if stages.get("confirm_schema") == "waiting":
        if (run_dir / "concepts" / "draft_schema.py").exists():
            confirm_schema(run_dir / "concepts" / "draft_schema.py", run_dir / "concepts" / "confirmed_schema.py")
            (run_dir / "intermediate" / "extraction_report.json").write_text(json.dumps({
                "total_instances": 18, "total_facts": 42, "total_relations": 16,
                "relation_types_used": ["operates_in_industry"], "avg_confidence": 0.87,
            }, indent=2), encoding="utf-8")
            # Round 2 supplementary web evidence reuses the same run dir (extract stage).
            _write_mock_web_evidence(
                run_dir, "web_002", "extract",
                "https://example.com/analytics-subdomains",
                "Analytics sub-domain taxonomy",
                "data analytics sub-domains",
            )
        emit({"type": "stage", "stage": "extract", "status": "running"})
        time.sleep(1.6)
        emit({"type": "stage", "stage": "solve", "status": "running"})
        time.sleep(1.6)
        emit({"type": "stage", "stage": "solve", "status": "done"})
        return (
            "Data analytics companies in the US include: Palantir, Databricks, and Snowflake."
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
                set_stage(current, "confirm_problem", "done")
                reset_stages_after(current, "confirm_problem")
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
            current = STORE.get(session_id)
            clarification = current.get("clarification", {})
            confirmed_problem = ""
            if isinstance(clarification, dict):
                confirmed_problem = str(clarification.get("problem", "")).strip()
            pipeline_question = confirmed_problem or content
            if MOCK_MODE:
                final = run_mock_agent(agent_input, session, emit)
            elif get_stage_status(session, "confirm_problem") != "done" and get_stage_status(session, "evidence") == "pending":
                final = run_problem_clarifier_agent(content, paths, session["thread_id"], session_run_dir(session), emit)
            elif get_stage_status(current, "confirm_schema") in ("waiting", "done"):
                final = run_solve_pipeline(pipeline_question, paths, current, emit)
            elif get_stage_status(current, "evidence") in ("pending", "running", "waiting") or get_stage_status(current, "schema_build") != "done":
                final = run_schema_pipeline(pipeline_question, paths, current, emit)
            else:
                final = run_real_agent(agent_input, session["thread_id"], session_run_dir(session), emit)
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
                    clarification = extract_clarification(final)
                    gate = infer_waiting_gate(session, final, clarification)
                    if gate:
                        already_waiting = any(
                            item.get("id") == gate and item.get("status") == "waiting"
                            for item in session.get("stages", [])
                        )
                        if not already_waiting:
                            set_stage(session, gate, "waiting")
                            reset_stages_after(session, gate)
                            await websocket.send_text(json.dumps(
                                {
                                    "type": "stage",
                                    "stage": gate,
                                    "status": "waiting",
                                    "label": stage_label(gate),
                                    "stages": session["stages"],
                                },
                                ensure_ascii=False,
                            ))
                        key = f"{gate}:waiting"
                        if key not in activity_seen:
                            activity_seen.add(key)
                            activity = stage_activity(gate, "waiting")
                            session["messages"].append(activity)
                            await websocket.send_text(json.dumps({"type": "activity", "message": activity}, ensure_ascii=False))
                        if gate == "confirm_problem" and clarification:
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
    elif payload_type == "confirm_problem":
        problem = str(payload.get("problem", "")).strip()
        steps = [str(item).strip() for item in (payload.get("steps") or []) if str(item).strip()]
        if not problem or not steps:
            error_message = ui_message("system", "The problem statement and at least one step are required.", tone="error")
            sink.events.append({"type": "error", "message": error_message})
        else:
            current = STORE.get(session["id"])
            set_stage(current, "confirm_problem", "done")
            reset_stages_after(current, "confirm_problem")
            current["clarification"] = {"problem": problem, "steps": steps, "status": "confirmed"}
            STORE.save(current)
            composed = (
                "I confirm the clarified problem.\n\n"
                f"**Question**: {problem}\n\n"
                "**Solution steps**:\n"
                + "\n".join(f"{index + 1}. {step}" for index, step in enumerate(steps))
            )
            await handle_chat(sink, session["id"], composed, [])
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
