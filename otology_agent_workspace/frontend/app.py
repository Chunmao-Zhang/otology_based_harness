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
from harness.ontology.json_contract import extract_json_object
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

# Human-gated mode: the ontology_coordinator LLM still drives the whole flow by
# delegating to its six subagents with the `task` tool (so the UI keeps showing
# which subagent is doing which task), but the backend splits the run into three
# self-contained segments separated by two human confirmation gates:
#   1. after the problem is clarified  -> confirm_problem (review/edit problem+steps)
#   2. after the schema is built/judged -> confirm_schema  (review/edit schema)
# The two confirm_* stages are gates, not subagents; every other stage maps 1:1
# to the subagent that owns it.
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
    "confirm_problem": "Waiting for you to confirm the clarified problem and steps…",
    "evidence": "Gathering evidence (reading uploads, searching the web if needed)…",
    "schema_build": "Building the ontology schema…",
    "schema_judge": "Judging whether the schema can answer the question…",
    "confirm_schema": "Waiting for you to confirm the ontology schema…",
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


def session_completed_run_dir(session: dict[str, Any]) -> Path | None:
    """Return the session's run dir if it holds a COMPLETED run (the solver wrote
    ``intermediate/solver_result.json``), else None. Used to detect a follow-up
    that should continue an existing run instead of starting a brand-new one."""
    if not session.get("run_id"):
        return None
    run_dir = session_run_dir(session)
    return run_dir if (run_dir / "intermediate" / "solver_result.json").exists() else None


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
    api_key_present = False
    serper_key_present = False
    try:
        from harness.config import load_config
        from harness.agents.registry import AgentRegistry

        cfg = load_config(str(ROOT / "harness.json"))
        registry = AgentRegistry(cfg)
        agent_cfg = registry.get(AGENT_ID)
        model_id = agent_cfg.model.model_id if agent_cfg.model else ""
        api_key_present = bool(agent_cfg.model and agent_cfg.model.api_key)
        serper_key_present = bool(os.environ.get("SERPER_API_KEY"))
    except Exception:
        pass
    return {
        "ok": True,
        "brand": UI_BRAND,
        "agent": AGENT_ID,
        "model": model_id,
        "api_key_present": api_key_present,
        "serper_key_present": serper_key_present,
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
    kind = kind.lower().strip()

    # Generated dataset files derived by build_dataset (the ABox). These are the
    # actual facts/relations the solver reads, served verbatim for download.
    data_files = {
        "facts": ("data/facts.csv", "facts.csv", "text/csv"),
        "relations_data": ("data/relations.csv", "relations.csv", "text/csv"),
        "instances": ("data/instances.json", "instances.json", "application/json"),
    }
    if kind in data_files:
        relative, filename, media_type = data_files[kind]
        target = run_dir / relative
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"{filename} has not been generated yet")
        return FileResponse(
            path=target,
            media_type=media_type,
            filename=filename,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    payload = schema_payload(run_dir)
    if kind == "python":
        filename = "ontology_schema.py"
        return Response(
            content=payload["schema_text"],
            media_type="text/x-python",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    form = payload.get("form") or []
    entities = [item for item in form if item.get("type") == "entity"]
    if kind == "entities":
        # Entity Definitions projection: one row per attribute (glossary columns).
        rows = ["entity_type,entity_data_type,attribute,attribute_data_type,optional"]
        for item in entities:
            etype = item.get("entity_type") or item.get("name", "")
            edata = item.get("entity_data_type") or item.get("id_type") or "str"
            attributes = item.get("attributes") or []
            if not attributes:
                rows.append(f'{csv_cell(etype)},{csv_cell(edata)},,,')
                continue
            for attr in attributes:
                rows.append(",".join([
                    csv_cell(etype),
                    csv_cell(edata),
                    csv_cell(attr.get("attribute") or attr.get("name", "")),
                    csv_cell(attr.get("attribute_data_type") or attr.get("value_type", "str")),
                    csv_cell("true" if attr.get("optional") else "false"),
                ]))
        filename = "entity_definitions.csv"
    elif kind == "relations":
        # Relation Schema projection: head_entity_type | relation_type | tail_entity_type.
        rows = ["head_entity_type,relation_type,tail_entity_type"]
        for item in (entry for entry in form if entry.get("type") == "relation"):
            rows.append(",".join([
                csv_cell(item.get("head_entity_type") or item.get("head_entity", "")),
                csv_cell(item.get("relation_type") or item.get("relation", "")),
                csv_cell(item.get("tail_entity_type") or item.get("tail_entity", "")),
            ]))
        filename = "relation_schema.csv"
    else:
        raise HTTPException(status_code=400, detail="Unknown schema artifact kind")
    return Response(
        content="\n".join(rows) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _read_csv_preview(path: Path, max_rows: int = 200) -> dict[str, Any]:
    """Parse a generated CSV into {columns, rows, total, truncated} for display."""
    import csv as _csv

    if not path.exists():
        return {"available": False, "columns": [], "rows": [], "total": 0, "truncated": False}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = _csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return {"available": True, "columns": [], "rows": [], "total": 0, "truncated": False}
        rows: list[list[str]] = []
        total = 0
        for record in reader:
            total += 1
            if len(rows) < max_rows:
                rows.append([str(cell) for cell in record])
    return {
        "available": True,
        "columns": [str(col) for col in header],
        "rows": rows,
        "total": total,
        "truncated": total > len(rows),
    }


@app.get("/api/dataset")
async def dataset_preview(session_id: str = ""):
    """Generated facts.csv / relations.csv contents for the Schema Studio display."""
    run_dir = resolve_artifact_dir(session_id, "data/facts.csv") or resolve_artifact_dir(
        session_id, "data/relations.csv"
    )
    if run_dir is None:
        return {"ok": True, "run_id": "", "facts": {"available": False}, "relations": {"available": False}}
    facts = _read_csv_preview(run_dir / "data" / "facts.csv")
    relations = _read_csv_preview(run_dir / "data" / "relations.csv")
    return {
        "ok": True,
        "run_id": run_dir.name,
        "facts": facts,
        "relations": relations,
        "has_instances": (run_dir / "data" / "instances.json").exists(),
    }


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
    now_ms = int(time.time() * 1000)
    for stage in session["stages"]:
        index = order.index(stage["id"])
        if index < target_index and stage["status"] in ("pending", "running", "waiting"):
            stage["status"] = "done"
        elif index == target_index:
            stage["status"] = status
            # Stamp when this step actually began working so the UI can show a
            # per-step elapsed timer that resets for each subagent, instead of a
            # single clock that runs for the whole segment.
            if status == "running" and not stage.get("started_at"):
                stage["started_at"] = now_ms


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


# Only the tail of the live reasoning/answer buffers is ever sent to the client.
# A full run can stream many thousands of model-thinking tokens per step; sending
# (and re-rendering) the whole growing buffer on every delta is what made long
# runs blow up the browser renderer. Capping the payload keeps it bounded.
STREAM_TAIL_CHARS = 6000


def _tail(text: str, limit: int = STREAM_TAIL_CHARS) -> str:
    if not isinstance(text, str) or len(text) <= limit:
        return text or ""
    return "…" + text[-limit:]


def _inputs_block(payload: dict[str, Any]) -> str:
    return "Inputs:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def _segment_message_clarify(question: str, upload_paths: list[str]) -> str:
    """Segment 1 — clarify only, then stop for the problem-confirmation gate."""
    payload = {"question": question, "upload_paths": upload_paths}
    return (
        "You are running a HUMAN-GATED ontology workflow. Run ONLY Step 1 "
        "(problem clarification) now, then STOP for human confirmation. Do NOT "
        "collect evidence, build a schema, extract data, or solve yet.\n\n"
        "Delegate to the `problem_clarifier` subagent with the `task` tool, "
        "passing the inputs below as JSON. When it returns "
        '{"problem": "...", "steps": [...]}, output EXACTLY that JSON object as '
        "your final message — no prose before or after it, and no other subagent "
        "calls. A human will review and may edit the problem and steps before you "
        "continue.\n\n"
        + _inputs_block(payload)
        + f"\n\n{USER_VISIBLE_OUTPUT_CONTRACT}"
    )


def _segment_message_schema(
    problem: str, steps: list[str], upload_paths: list[str], workspace_dir: str, run_id: str
) -> str:
    """Segment 2 — evidence + schema build/judge, then stop for the schema gate."""
    payload = {
        "question": problem,
        "upload_paths": upload_paths,
        "workspace_dir": workspace_dir,
        "run_id": run_id,
    }
    steps_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps)) or "(none provided)"
    return (
        "You are continuing a HUMAN-GATED ontology workflow. The human has "
        "CONFIRMED the problem statement and solution steps below — treat them as "
        "final and do NOT re-clarify them.\n\n"
        "Run Step 2 (`evidence_collector`), Step 3 (`schema_builder`), then Step 4 "
        "(`schema_judger`) by delegating with the `task` tool. If the judger says "
        "the schema is not answerable, call `schema_builder` once more to patch and "
        "re-save, then judge again. After at most one patch, STOP. Do NOT extract "
        "data or solve — a human will review and may edit the schema first.\n\n"
        "When the schema is saved and validated, output a short JSON object "
        '{"schema_outline": [...], "confirmed_schema_path": "<path schema_builder '
        'returned>"} as your final message. Do not include draft code, answerability '
        "judgments, evidence counts, or file paths in any user-facing prose.\n\n"
        f"Confirmed problem:\n{problem}\n\nConfirmed steps:\n{steps_text}\n\n"
        + _inputs_block(payload)
        + f"\n\n{USER_VISIBLE_OUTPUT_CONTRACT}"
    )


def _segment_message_solve(
    problem: str,
    upload_paths: list[str],
    workspace_dir: str,
    run_id: str,
    confirmed_schema_path: str,
    schema_outline_data: list[dict[str, Any]],
    evidence_manifest_path: str,
    instances_path: str,
) -> str:
    """Segment 3 — extract + solve against the human-confirmed schema."""
    payload = {
        "question": problem,
        "upload_paths": upload_paths,
        "workspace_dir": workspace_dir,
        "run_id": run_id,
        "confirmed_schema_path": confirmed_schema_path,
        "schema_outline": schema_outline_data,
        "evidence_manifest_path": evidence_manifest_path,
        "instances_path": instances_path,
    }
    return (
        "You are COMPLETING a HUMAN-GATED ontology workflow. The human has "
        "CONFIRMED (and may have EDITED) the ontology schema. The final schema is "
        "already saved at `confirmed_schema_path` below — DO NOT rebuild, re-judge, "
        "or modify the schema, and DO NOT re-run clarification or evidence "
        "collection.\n\n"
        "Run Step 5 (`data_extractor`) using the confirmed schema and the "
        "`schema_outline` below (pass it through verbatim), then Step 6 "
        "(`workspace_solver`). Make sure `workspace_solver` writes "
        f"`{workspace_dir}/intermediate/solver_result.json` before you answer. "
        "Then give the final answer to the user in concise Chinese — the direct "
        "answer only.\n\n"
        f"Confirmed problem:\n{problem}\n\n"
        + _inputs_block(payload)
        + f"\n\n{USER_VISIBLE_OUTPUT_CONTRACT}"
    )


def _segment_message_continue(
    follow_up: str,
    prior_answer: str,
    upload_paths: list[str],
    workspace_dir: str,
    run_id: str,
    confirmed_schema_path: str,
    schema_outline_data: list[dict[str, Any]],
    evidence_manifest_path: str,
    instances_path: str,
) -> str:
    """Continuation segment — the user is following up on an ALREADY-COMPLETED
    run (expand / add / refine / re-angle). Reuse the existing run's workspace
    and let the coordinator call only the subagent(s) the request needs."""
    payload = {
        "question": follow_up,
        "upload_paths": upload_paths,
        "workspace_dir": workspace_dir,
        "run_id": run_id,
        "confirmed_schema_path": confirmed_schema_path,
        "schema_outline": schema_outline_data,
        "evidence_manifest_path": evidence_manifest_path,
        "instances_path": instances_path,
    }
    return (
        "You are CONTINUING an ontology workflow run that has ALREADY COMPLETED. "
        "The user is following up on that finished result (their request is "
        "below) — typically to expand it, add more, go deeper, include another "
        "facet, or re-organize/re-angle it. This is NOT a brand-new run: reuse "
        "the existing run's workspace, schema, evidence manifest, and instances "
        "at the paths in the inputs below. Do NOT restart from Step 1 and do NOT "
        "answer from memory.\n\n"
        "Follow your 'Handling other / follow-up needs' instructions: compare "
        "what the user now wants against what the run already produced, then "
        "delegate to the MINIMAL set of subagents that closes the gap, in "
        "dependency order — `evidence_collector` for more raw evidence, "
        "`schema_builder` (PATCH mode) then `schema_judger` if the schema cannot "
        "represent the new ask, `data_extractor` to extend the structured data, "
        "and/or `workspace_solver` to (re)compute. Always finish by having "
        "`workspace_solver` write "
        f"`{workspace_dir}/intermediate/solver_result.json`, then answer the user "
        "in concise Chinese grounded in the workspace data — never fabricate the "
        "added content from memory.\n\n"
        f"The user's follow-up request:\n{follow_up}\n\n"
        f"Your previous answer to them:\n{prior_answer or '(unavailable)'}\n\n"
        + _inputs_block(payload)
        + f"\n\n{USER_VISIBLE_OUTPUT_CONTRACT}"
    )


def extract_clarification(text: str) -> dict[str, Any] | None:
    """Pull a structured {problem, steps} out of a clarification reply."""
    try:
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
    for line in (text or "").splitlines():
        stripped = line.strip()
        match = re.match(r"^\*{0,2}(?:Question|问题|核心问题)\s*[:：]\*{0,2}\s*(.+)$", stripped)
        if match and not problem:
            problem = match.group(1).strip()
            continue
        match = re.match(r"^\d+[.、)]\s*(.+)$", stripped)
        if match:
            steps.append(match.group(1).strip())
    if problem and steps:
        return {"problem": problem, "steps": steps}
    return None


def open_schema_gate(run_dir: Path) -> None:
    """Present the freshly built schema as an editable DRAFT for the schema gate.

    ``save_schema`` writes both ``draft_schema.py`` and ``confirmed_schema.py``;
    removing the confirmed copy makes ``/api/schema`` serve the draft so edits via
    Schema Studio round-trip correctly until the human confirms."""
    confirmed = run_dir / "concepts" / "confirmed_schema.py"
    draft = run_dir / "concepts" / "draft_schema.py"
    if confirmed.exists():
        if not draft.exists():
            draft.write_text(confirmed.read_text(encoding="utf-8"), encoding="utf-8")
        confirmed.unlink()


def finalize_schema(run_dir: Path) -> str:
    """Promote the (possibly human-edited) draft to the confirmed schema and
    rebuild the workspace so extraction runs against exactly what the user saw.

    Returns the confirmed schema path."""
    concepts = run_dir / "concepts"
    draft = concepts / "draft_schema.py"
    confirmed = concepts / "confirmed_schema.py"
    if draft.exists():
        confirm_schema(draft, confirmed)
    if confirmed.exists():
        build_workspace(str(run_dir), str(confirmed))
    return str(confirmed)


def _prepare_run_dir(run_dir: Path) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("concepts", "data", "src", "intermediate", "intermediate/web_evidence"):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    os.environ["HARNESS_ROOT"] = str(ROOT)
    os.environ["HARNESS_AGENT_ID"] = AGENT_ID
    os.environ["HARNESS_RUN_DIR"] = str(run_dir.resolve())


def _drive_coordinator(message: str, thread_id: str, run_dir: Path, emit) -> str:
    """Stream one coordinator segment and return its final, tool-call-free message.

    The coordinator LLM owns the flow within a segment: it delegates to subagents
    with the ``task`` tool, and the backend only streams what happens. There is no
    Python state machine, hardcoded routing, or fallback — the segment boundaries
    (the two human gates) are enforced by *which* message the backend hands the
    coordinator, not by parsing its prose. The UI events carry an explicit
    coordinator-vs-subagent identity so a viewer can see which agent is doing what:

    - the coordinator's own reasoning streams on the virtual ``__coordinator__``
      lane (it is the orchestrator, not a stage);
    - each ``task`` delegation flips the corresponding stage to ``running`` and
      tags the stage event with the responsible subagent;
    - a subagent's own model output / tool calls stream on that subagent's stage.
    """
    from langchain_core.messages import HumanMessage

    agent, _agent_cfg = get_cached_coordinator_agent()
    _prepare_run_dir(run_dir)
    run_id = run_dir.name

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
        emit({"type": "stream", "stage": lane, "thinking": _tail(thinking_buf), "output": _tail(output_buf)})

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


def run_segment_clarify(
    question: str, upload_paths: list[str], thread_id: str, run_dir: Path, emit
) -> str:
    """Segment 1: clarify the problem, then stop for the problem gate."""
    message = _segment_message_clarify(question, upload_paths)
    return _drive_coordinator(message, thread_id, run_dir, emit)


def run_segment_schema(
    problem: str, steps: list[str], upload_paths: list[str], thread_id: str, run_dir: Path, emit
) -> str:
    """Segment 2: collect evidence + build/judge schema, then stop for the schema gate."""
    message = _segment_message_schema(problem, steps, upload_paths, vrun_for(run_dir), run_dir.name)
    return _drive_coordinator(message, thread_id, run_dir, emit)


def run_segment_solve(
    problem: str, upload_paths: list[str], thread_id: str, run_dir: Path, emit
) -> str:
    """Segment 3: extract + solve against the human-confirmed schema, then answer."""
    vrun = vrun_for(run_dir)
    confirmed = run_dir / "concepts" / "confirmed_schema.py"
    try:
        outline = schema_outline(str(confirmed)) if confirmed.exists() else []
    except Exception:
        outline = []
    # Subagent file tools resolve paths under the harness root via the virtual
    # /runs/... namespace, so every path handed to the coordinator must be the
    # virtual root-relative path — never the absolute on-disk path (which the
    # tools would re-root, nesting it under the harness root).
    message = _segment_message_solve(
        problem,
        upload_paths,
        vrun,
        run_dir.name,
        f"{vrun}/concepts/confirmed_schema.py",
        outline,
        f"{vrun}/intermediate/evidence_manifest.json",
        f"{vrun}/data/instances.json",
    )
    return _drive_coordinator(message, thread_id, run_dir, emit)


def run_segment_continue(
    follow_up: str, prior_answer: str, upload_paths: list[str], thread_id: str, run_dir: Path, emit
) -> str:
    """Continuation: reuse a completed run's workspace; the coordinator picks the
    minimal subagent(s), re-solves, and answers."""
    vrun = vrun_for(run_dir)
    confirmed = run_dir / "concepts" / "confirmed_schema.py"
    try:
        outline = schema_outline(str(confirmed)) if confirmed.exists() else []
    except Exception:
        outline = []
    # `workspace_solver` writes src/solve.py with write_file, which cannot
    # overwrite an existing file, so clear the previous run's solver artifacts to
    # let a re-solve write fresh (mirrors open_schema_gate clearing the schema).
    for stale in (run_dir / "src" / "solve.py", run_dir / "intermediate" / "solver_result.json"):
        if stale.exists():
            stale.unlink()
    message = _segment_message_continue(
        follow_up,
        prior_answer,
        upload_paths,
        vrun,
        run_dir.name,
        f"{vrun}/concepts/confirmed_schema.py",
        outline,
        f"{vrun}/intermediate/evidence_manifest.json",
        f"{vrun}/data/instances.json",
    )
    return _drive_coordinator(message, thread_id, run_dir, emit)


# ---------------------------------------------------------------------------
# General conversation + gate-intent routing (keeps the architecture general)
#
# The coordinator/subagents only run the heavy ontology pipeline. But this is a
# *general* agent architecture: ordinary chat — greetings, follow-up questions,
# side questions while a gate is open — must be answered directly without forcing
# the clarify->evidence->schema->extract->solve flow. And at a human gate a typed
# message may be a confirmation, a revision request, or an unrelated question.
# We never hardcode this routing with string matching or a Python state machine:
# we ask the same model that powers the coordinator to classify the message, then
# act on its decision. The model owns every decision.
# ---------------------------------------------------------------------------


def _aux_llm():
    """Build a bare (tool-less) chat model from the coordinator's model config.

    Used for lightweight intent classification and direct general answers. It is
    the same provider/model the coordinator runs on, built through the shared
    ``_build_model`` helper, so no separate configuration is required."""
    from harness.agents.agent_loop import _build_model

    _agent, agent_cfg = get_cached_coordinator_agent()
    return _build_model(agent_cfg.model)


def _recent_dialogue(session: dict[str, Any], limit: int = 12) -> list[tuple[str, str]]:
    """Return the last ``limit`` (role, content) user/assistant turns for context."""
    turns: list[tuple[str, str]] = []
    for message in session.get("messages", []):
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        turns.append((role, content))
    return turns[-limit:]


def _classify_intent(
    content: str, context: str, session: dict[str, Any], allow_continue: bool = False
) -> str:
    """Classify a free-text chat message at a decision point, via the model.

    ``context`` is one of:
      - ``"idle"``: no gate open. Returns ``"pipeline"`` (the question needs the
        ontology schema-building workflow) or ``"general"`` (answer directly).
        When ``allow_continue`` is set (a completed run exists for this session),
        ``"continue"`` is also allowed (follow up on / expand the finished run).
      - ``"confirm_problem"`` / ``"confirm_schema"``: a human gate is open.
        Returns ``"confirm"``, ``"revise"``, or ``"general"``.

    Falls back conservatively if the model or parsing fails (idle->``"general"``;
    gates->``"revise"``) so we never silently auto-confirm or force the pipeline
    on an ambiguous message."""
    import traceback as _tb

    if context == "idle" and allow_continue:
        allowed = ("continue", "pipeline", "general")
        guide = (
            "The user just sent a new message and no confirmation gate is open. A "
            "previous structured run for this session has ALREADY COMPLETED and its "
            "result was delivered. Classify the new message:\n"
            "- `continue`: the user is following up on that just-completed result "
            "to expand it, add more, go deeper, include another facet, or "
            "re-organize/re-angle it, on the SAME overall subject (e.g. "
            "'\u8fd8\u4e0d\u591f\uff0c\u591a\u6269\u5145\u4e00\u70b9\u5b66\u672f\u8bba\u6587\u76f8\u5173\u7684\u5185\u5bb9', '\u518d\u591a\u5217\u4e00\u4e9b\u4ed6\u7684\u8bba\u6587', "
            "'\u4e5f\u628a\u4ed6\u7684\u4e13\u5229\u4fe1\u606f\u52a0\u8fdb\u53bb', '\u628a\u7b54\u6848\u6309\u65f6\u95f4\u7ebf\u91cd\u65b0\u6574\u7406'). Reuse the "
            "existing run and its data.\n"
            "- `pipeline`: the user starts a DIFFERENT structured task on a new "
            "subject that needs its own evidence/schema/extraction — not a "
            "continuation of the previous result.\n"
            "- `general`: greetings, chit-chat, opinions, definitions, "
            "explanations, or coding/math help that needs no workspace data."
        )
    elif context == "idle":
        allowed = ("pipeline", "general")
        guide = (
            "The user just sent a new message and no confirmation gate is open. "
            "Decide whether answering it REQUIRES the structured ontology workflow "
            "(gather external evidence, design an ontology schema of multiple "
            "entity/relation types, extract structured instances, then compute an "
            "answer) -> reply `pipeline`; or whether it is ordinary conversation "
            "that should be answered directly -> reply `general`.\n"
            "Choose `pipeline` ONLY when the request is to collect, organize, or "
            "compare information about real-world entities into a structured result "
            "(e.g. 'list the data-analytics companies operating in the US with "
            "their sub-domains', 'organize this person's papers and academic "
            "activity over the last decade', or a multi-hop join over entities). "
            "Choose `general` for greetings, chit-chat, opinions, definitions, "
            "explanations, coding/math help, follow-up questions about the previous "
            "answer, or any question a knowledgeable assistant can answer in prose."
        )
    elif context == "confirm_problem":
        allowed = ("confirm", "revise", "general")
        guide = (
            "The assistant proposed a clarified problem statement and solution "
            "steps and is waiting for the user. Classify the user's message:\n"
            "- `confirm`: the user approves the problem/steps and wants to proceed "
            "(e.g. 'ok', 'confirm', 'continue', 'looks good', '\u53ef\u4ee5', '\u7ee7\u7eed').\n"
            "- `revise`: the user wants to change the problem or the steps (e.g. "
            "'also include ...', 'change step 2 to ...', 'I actually meant ...', "
            "'\u628a\u7b2c\u4e09\u6b65\u6539\u6210...').\n"
            "- `general`: the user asks an unrelated/side question or small talk "
            "to be answered without changing or confirming the plan."
        )
    else:  # confirm_schema
        allowed = ("confirm", "revise", "general")
        guide = (
            "The assistant built an ontology schema (entities + relations) and is "
            "waiting for the user to confirm it before extracting data. Classify "
            "the user's message:\n"
            "- `confirm`: the user approves the schema and wants to proceed (e.g. "
            "'ok', 'confirm', 'continue', 'looks good', '\u786e\u8ba4', '\u6ca1\u95ee\u9898').\n"
            "- `revise`: the user wants to change the schema -- add/remove/rename "
            "an entity, attribute, or relation, change a relation's head/tail/type, "
            "etc. (e.g. 'add an entity for ...', 'the relation should be ...', "
            "'\u628aX\u6539\u6210\u5c5e\u6027', '\u52a0\u4e00\u4e2a\u5173\u7cfb...').\n"
            "- `general`: the user asks an unrelated/side question or small talk "
            "to be answered without changing or confirming the schema."
        )
    from langchain_core.messages import HumanMessage, SystemMessage

    history = _recent_dialogue(session, limit=8)
    history_text = "\n".join(f"{role.upper()}: {text}" for role, text in history) or "(none)"
    system = (
        "You are an intent classifier inside an ontology QA agent. Read the recent "
        "conversation and the user's latest message, then output ONLY one word "
        f"from this set: {', '.join(allowed)}. No punctuation, no explanation.\n\n"
        + guide
    )
    prompt = (
        f"Recent conversation:\n{history_text}\n\n"
        f"User's latest message:\n{content}\n\n"
        f"Answer with exactly one of: {', '.join(allowed)}."
    )
    fallback = "general" if context == "idle" else "revise"
    try:
        llm = _aux_llm()
        reply = llm.invoke([SystemMessage(content=system), HumanMessage(content=prompt)])
        text = str(getattr(reply, "content", "") or "").strip().lower()
        for token in allowed:
            if token in text:
                return token
        return fallback
    except Exception:
        _tb.print_exc()
        return fallback


GENERAL_SYSTEM_PROMPT = (
    "You are the assistant of a general-purpose, ontology-based QA agent. For this "
    "turn you are answering the user directly in normal conversation -- you are "
    "NOT running the structured ontology pipeline (no evidence collection, schema "
    "building, extraction, or solver). Answer helpfully, accurately, and "
    "concisely. If you are uncertain, or the question needs fresh external data "
    "you do not have, say so briefly. Reply in the user's language (default to "
    "Chinese if the user wrote Chinese). Do not mention internal tools, subagents, "
    "file paths, or pipeline steps."
)


def run_general_answer(content: str, history: list[tuple[str, str]], emit) -> str:
    """Answer a general/non-pipeline message directly with a bare LLM call.

    Streams tokens on the coordinator lane so the live card behaves like a normal
    reply, and returns the final answer text. No stages, subagents, or schema --
    this is what keeps the architecture general for ordinary questions."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    messages: list[Any] = [SystemMessage(content=GENERAL_SYSTEM_PROMPT)]
    for role, text in history:
        messages.append(HumanMessage(content=text) if role == "user" else AIMessage(content=text))
    messages.append(HumanMessage(content=content))

    llm = _aux_llm()
    thinking_buf = ""
    output_buf = ""
    last_emit = 0.0

    def push(force: bool = False) -> None:
        nonlocal last_emit
        now = time.monotonic()
        if not force and (now - last_emit) < 0.35:
            return
        last_emit = now
        emit({
            "type": "stream",
            "stage": COORDINATOR_LANE,
            "thinking": _tail(thinking_buf),
            "output": _tail(output_buf),
        })

    try:
        for chunk in llm.stream(messages):
            thinking_buf, output_buf = _accumulate_stream(chunk, thinking_buf, output_buf)
            push()
    except Exception:
        import traceback as _tb
        _tb.print_exc()
        reply = llm.invoke(messages)
        output_buf = str(getattr(reply, "content", "") or "")
    push(force=True)
    return output_buf.strip()


def _segment_message_clarify_revise(
    question: str,
    prior_problem: str,
    prior_steps: list[str],
    revision: str,
    upload_paths: list[str],
) -> str:
    """Coordinator message for re-running Step 1 to REVISE the problem/steps."""
    payload = {
        "question": question,
        "upload_paths": upload_paths,
        "prior": {"problem": prior_problem, "steps": prior_steps},
        "revision": revision,
    }
    steps_text = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(prior_steps)) or "(none)"
    return (
        "You are running a HUMAN-GATED ontology workflow at the problem-"
        "clarification gate. You previously proposed the problem and steps below, "
        "and the human has now asked you to REVISE them. Run ONLY Step 1 (problem "
        "clarification) again via the `problem_clarifier` subagent, passing it the "
        "inputs JSON (which includes `prior` and `revision`) so it returns an "
        'UPDATED {"problem": "...", "steps": [...]}. Apply the human\'s revision '
        "faithfully and keep everything else stable. Then STOP and output EXACTLY "
        "that JSON object as your final message -- no prose, no other subagent "
        "calls.\n\n"
        f"Previously proposed problem:\n{prior_problem}\n\n"
        f"Previously proposed steps:\n{steps_text}\n\n"
        f"Human's requested changes:\n{revision}\n\n"
        + _inputs_block(payload)
        + f"\n\n{USER_VISIBLE_OUTPUT_CONTRACT}"
    )


def _segment_message_schema_revise(
    problem: str,
    revision: str,
    upload_paths: list[str],
    workspace_dir: str,
    run_id: str,
    manifest_path: str,
    draft_schema_path: str,
) -> str:
    """Coordinator message for PATCHING the schema per the human's request."""
    payload = {
        "question": problem,
        "upload_paths": upload_paths,
        "workspace_dir": workspace_dir,
        "run_id": run_id,
        "evidence_manifest_path": manifest_path,
        "schema_path": draft_schema_path,
        "missing_requirements": [revision],
    }
    return (
        "You are continuing a HUMAN-GATED ontology workflow at the schema gate. "
        "You already built the ontology schema saved at `schema_path` below, and "
        "the human has reviewed it and requested the changes below. Apply them by "
        "delegating with the `task` tool: call `schema_builder` in PATCH mode -- "
        "pass it `schema_path`, `evidence_manifest_path`, and the human's changes "
        "in `missing_requirements` -- so it edits the existing schema (do NOT "
        "rebuild from scratch and do NOT re-run evidence collection) and re-saves "
        "it via `save_schema`. Then call `schema_judger` once. After at most one "
        "judge/patch cycle, STOP. Do NOT extract data or solve -- the human will "
        "review the revised schema again.\n\n"
        "When the revised schema is saved and validated, output a short JSON object "
        '{"schema_outline": [...], "confirmed_schema_path": "<path>"} as your final '
        "message. Do not include draft code, judgments, or file paths in any "
        "user-facing prose.\n\n"
        f"Confirmed problem:\n{problem}\n\n"
        f"Human's requested schema changes:\n{revision}\n\n"
        + _inputs_block(payload)
        + f"\n\n{USER_VISIBLE_OUTPUT_CONTRACT}"
    )


def run_segment_clarify_revise(
    question: str,
    prior_problem: str,
    prior_steps: list[str],
    revision: str,
    upload_paths: list[str],
    thread_id: str,
    run_dir: Path,
    emit,
) -> str:
    """Re-run Step 1 with the human's revision, then re-open the problem gate."""
    message = _segment_message_clarify_revise(question, prior_problem, prior_steps, revision, upload_paths)
    return _drive_coordinator(message, thread_id, run_dir, emit)


def run_segment_schema_revise(
    problem: str, revision: str, upload_paths: list[str], thread_id: str, run_dir: Path, emit
) -> str:
    """Patch the schema with the human's revision, then re-open the schema gate."""
    vrun = vrun_for(run_dir)
    message = _segment_message_schema_revise(
        problem,
        revision,
        upload_paths,
        vrun,
        run_dir.name,
        f"{vrun}/intermediate/evidence_manifest.json",
        f"{vrun}/concepts/draft_schema.py",
    )
    return _drive_coordinator(message, thread_id, run_dir, emit)


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
            ptype = payload.get("type")
            if ptype == "chat":
                await handle_chat(
                    websocket,
                    session["id"],
                    str(payload.get("content", "")),
                    list(payload.get("upload_ids", []) or []),
                )
            elif ptype == "confirm_problem":
                await handle_confirm_problem(
                    websocket,
                    session["id"],
                    str(payload.get("problem", "")),
                    [str(item) for item in (payload.get("steps") or [])],
                )
            elif ptype == "confirm_schema":
                await handle_confirm_schema(websocket, session["id"])
            elif ptype == "revise_schema":
                await handle_revise_schema(
                    websocket, session["id"], str(payload.get("instruction", ""))
                )
            elif ptype == "revise_problem":
                await handle_revise_problem(
                    websocket, session["id"], str(payload.get("instruction", ""))
                )
            elif ptype == "history":
                await websocket.send_text(
                    json.dumps({"type": "history", "session": STORE.get(session["id"])}, ensure_ascii=False)
                )
    except WebSocketDisconnect:
        return


def stage_status(session: dict[str, Any], stage_id: str) -> str:
    for stage in session.get("stages", []):
        if stage.get("id") == stage_id:
            return stage.get("status", "")
    return ""


def resolve_upload_paths(session_id: str, session: dict[str, Any], upload_names: list[str]) -> list[str]:
    if upload_names:
        upload_root = Path("otology_agent_workspace/data/uploads") / safe_session_id(session_id)
        paths = [str(upload_root / name) for name in upload_names]
        session["upload_paths"] = paths
        STORE.save(session)
        return paths
    stored = session.get("upload_paths", [])
    return [str(item) for item in stored if str(item).strip()] if isinstance(stored, list) else []


async def _emit_run_start(websocket: Any, session_id: str, note: str) -> None:
    """Tell the client a working segment has begun (resets the live banner) and
    log a start-of-run activity so the coordinator banner shows immediately."""
    await websocket.send_text(json.dumps({"type": "run_start"}, ensure_ascii=False))
    session = STORE.get(session_id)
    start_activity = ui_message(
        "event", note, kind="run_start", title="Start processing", status="running",
    )
    session["messages"].append(start_activity)
    STORE.save(session)
    await websocket.send_text(json.dumps({"type": "activity", "message": start_activity}, ensure_ascii=False))


async def _send_assistant_final(websocket: Any, session: dict[str, Any], message: dict[str, Any]) -> None:
    # The final message is already persisted to STORE before this call, so if the
    # socket dropped we just skip the push; the client recovers it via `history`.
    try:
        await websocket.send_text(json.dumps(
            {"type": "assistant_final", "message": message, "stages": session["stages"]},
            ensure_ascii=False,
        ))
    except (WebSocketDisconnect, RuntimeError):
        pass


async def _stream_run(websocket: Any, session_id: str, runner, on_done) -> None:
    """Run one coordinator segment in the executor, forward its stream/activity/
    stage events to the client, and hand the final message to ``on_done``.

    ``runner(emit) -> str`` performs the (blocking) segment; ``on_done(session_id,
    final) -> str | None`` finalizes it (persists + sends assistant_final) and
    returns an error string if the segment produced nothing usable."""
    activity_seen: set[str] = set()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit(event: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, event)

    def run_thread() -> None:
        try:
            final = runner(emit)
            emit({"type": "_done", "final": final})
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI as a friendly error.
            import traceback
            traceback.print_exc()
            sys.stderr.flush()
            emit({"type": "_error", "error": f"{type(exc).__name__}: {exc}"})

    future = loop.run_in_executor(EXECUTOR, run_thread)
    run_error: str | None = None

    # The client socket can drop mid-run (e.g. a tunnel idle-timeout on a long
    # model step). When that happens we must NOT abort the run: we keep draining
    # the queue so the segment finishes and every result is persisted to STORE,
    # so a reconnecting client recovers the full state via `history`. Sends to a
    # dead socket are swallowed instead of crashing the handler.
    connected = True

    async def safe_send(payload: dict[str, Any]) -> None:
        nonlocal connected
        if not connected:
            return
        try:
            await websocket.send_text(json.dumps(payload, ensure_ascii=False))
        except (WebSocketDisconnect, RuntimeError):
            # RuntimeError("WebSocket is not connected") is what Starlette raises
            # once the peer has gone away; treat it the same as a disconnect.
            connected = False

    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=900)
        except asyncio.TimeoutError:
            run_error = "The run timed out. Please retry or narrow the question."
            break

        etype = event["type"]
        if etype == "_done":
            run_error = await on_done(session_id, str(event.get("final", "")).strip())
            break

        if etype == "_error":
            run_error = "Something went wrong during the run. Please retry later."
            break

        if etype == "stream":
            # Live token stream for the active lane. Ephemeral: forwarded to
            # the client for the live card but not persisted to history.
            await safe_send({
                "type": "stream",
                "stage": event.get("stage", ""),
                "thinking": redact_paths(event.get("thinking", "")),
                "output": redact_paths(event.get("output", "")),
            })
            continue

        if etype == "activity":
            session = STORE.get(session_id)
            message = event.get("message")
            if isinstance(message, dict):
                session["messages"].append(message)
                STORE.save(session)
                await safe_send({"type": "activity", "message": message})
            continue

        if etype == "stage":
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
            await safe_send({
                "type": "stage",
                "stage": event["stage"],
                "status": status,
                "label": stage_label(event["stage"]),
                "detail": STAGE_RUNNING_TEXT.get(event["stage"], ""),
                "agent": event.get("agent", ""),
                "agent_label": event.get("agent_label", ""),
                "stages": session["stages"],
            })
            if activity is not None:
                await safe_send({"type": "activity", "message": activity})

    if run_error:
        session = STORE.get(session_id)
        error_message = ui_message("system", run_error, tone="error")
        session["messages"].append(error_message)
        STORE.save(session)
        await safe_send({"type": "error", "message": error_message})

    await safe_send({"type": "run_done"})


async def handle_chat(websocket: Any, session_id: str, content: str, upload_ids: list[str]) -> None:
    content = content.strip()
    if not content:
        return

    session = STORE.get(session_id)

    # ---- A human confirmation gate is open: the model decides what the typed
    # message means (confirm / revise / unrelated question). We never assume a
    # bare chat at a gate is a confirmation -- that broke robustness before. ----
    schema_waiting = stage_status(session, "confirm_schema") == "waiting"
    problem_waiting = stage_status(session, "confirm_problem") == "waiting"
    if schema_waiting or problem_waiting:
        gate = "confirm_schema" if schema_waiting else "confirm_problem"
        intent = _classify_intent(content, gate, session)
        # Echo the user's actual words so the transcript reflects what they said.
        user_message = ui_message("user", content)
        session["messages"].append(user_message)
        STORE.save(session)
        await websocket.send_text(json.dumps({"type": "message", "message": user_message}, ensure_ascii=False))
        if intent == "confirm":
            if gate == "confirm_schema":
                await handle_confirm_schema(websocket, session_id, echo=False)
            else:
                clar = session.get("clarification") or {}
                await handle_confirm_problem(
                    websocket,
                    session_id,
                    str(clar.get("problem") or ""),
                    [str(item) for item in (clar.get("steps") or [])],
                    echo=False,
                )
        elif intent == "revise":
            if gate == "confirm_schema":
                await handle_revise_schema(websocket, session_id, content, echo=False)
            else:
                await handle_revise_problem(websocket, session_id, content, echo=False)
        else:  # general: answer the side question, keep the gate open.
            await handle_general(websocket, session_id, content, reopen_gate=gate)
        return

    # ---- No gate open: the model decides whether this needs the structured
    # ontology pipeline, is a follow-up that should CONTINUE the completed run,
    # or is ordinary conversation to answer directly. ----
    upload_names = [Path(str(item)).name for item in upload_ids if str(item).strip()]
    # A continuation only makes sense when a previous run finished and the user
    # is not attaching new files (new uploads mean fresh source material).
    completed_run = session_completed_run_dir(session)
    allow_continue = completed_run is not None and not upload_names
    intent = _classify_intent(content, "idle", session, allow_continue=allow_continue)
    if intent == "continue" and allow_continue:
        user_message = ui_message("user", content)
        session["messages"].append(user_message)
        STORE.save(session)
        await websocket.send_text(json.dumps({"type": "message", "message": user_message}, ensure_ascii=False))
        await handle_continue(websocket, session_id, content)
        return
    if intent == "general" and not upload_names:
        user_message = ui_message("user", content)
        session["messages"].append(user_message)
        if sum(1 for item in session["messages"] if item.get("role") == "user") == 1:
            session["title"] = content[:42] + ("..." if len(content) > 42 else "")
        STORE.save(session)
        await websocket.send_text(json.dumps({"type": "message", "message": user_message}, ensure_ascii=False))
        await handle_general(websocket, session_id, content, reopen_gate=None)
        return

    # ---- Pipeline: brand-new structured question -- reset for a fresh run. ----
    user_message = ui_message("user", content, uploads=upload_names)
    session["messages"].append(user_message)
    if sum(1 for item in session["messages"] if item.get("role") == "user") == 1:
        session["title"] = content[:42] + ("..." if len(content) > 42 else "")
    session["stages"] = fresh_stages()
    session["clarification"] = None
    run_id = f"sess-{safe_session_id(session_id)}-{int(time.time() * 1000)}"
    session["run_id"] = run_id
    paths = resolve_upload_paths(session_id, session, upload_names)
    STORE.save(session)
    await websocket.send_text(json.dumps({"type": "message", "message": user_message}, ensure_ascii=False))
    await _emit_run_start(websocket, session_id, "Question received. Clarifying the problem…")

    run_dir = RUNS_DIR / run_id

    def runner(emit) -> str:
        return run_segment_clarify(content, paths, f"{run_id}:{AGENT_ID}:clarify", run_dir, emit)

    async def on_done(sid: str, final: str) -> str | None:
        return await _finalize_problem_gate(websocket, sid, final)

    await _stream_run(websocket, session_id, runner, on_done)


async def handle_continue(websocket: Any, session_id: str, content: str) -> None:
    """A follow-up on an ALREADY-COMPLETED run: reuse the existing run's
    workspace and let the coordinator call only the subagent(s) the request
    needs (gather more evidence, patch the schema, extend the data, and/or
    re-solve), then answer. We neither reset the stages nor create a new run —
    this is the non-rigid path for 'supplement / expand / re-angle' follow-ups.
    The caller has already echoed the user's message."""
    session = STORE.get(session_id)
    run_id = session.get("run_id", "")
    if not run_id:
        return
    run_dir = RUNS_DIR / run_id
    prior_answer = ""
    for message in reversed(session.get("messages", [])):
        if message.get("role") == "assistant" and message.get("content"):
            prior_answer = str(message["content"]).strip()
            break
    paths = resolve_upload_paths(session_id, session, [])
    await _emit_run_start(websocket, session_id, "Continuing on your existing result…")

    def runner(emit) -> str:
        return run_segment_continue(
            content, prior_answer, paths, f"{run_id}:{AGENT_ID}:continue", run_dir, emit
        )

    async def on_done(sid: str, final: str) -> str | None:
        return await _finalize_answer(websocket, sid, final)

    await _stream_run(websocket, session_id, runner, on_done)


async def handle_confirm_problem(
    websocket: Any, session_id: str, problem: str, steps: list[str], echo: bool = True
) -> None:
    """The user confirmed (and may have edited) the clarified problem + steps.
    Persist them and run segment 2 (evidence + schema), then open the schema gate.

    ``echo`` controls whether we append a canned confirmation bubble. It is True
    for the explicit Confirm button (which carries no user text) and False when
    the caller already echoed the user's own words (free-text confirm at the gate)."""
    problem = problem.strip()
    steps = [str(item).strip() for item in steps if str(item).strip()]
    session = STORE.get(session_id)
    run_id = session.get("run_id", "")
    if not problem or not steps or not run_id:
        return

    session["clarification"] = {"problem": problem, "steps": steps, "status": "confirmed"}
    set_stage(session, "confirm_problem", "done")
    paths = resolve_upload_paths(session_id, session, [])
    if echo:
        confirm_message = ui_message("user", "Confirmed the problem and solution steps. Please continue.")
        session["messages"].append(confirm_message)
        await websocket.send_text(json.dumps({"type": "message", "message": confirm_message}, ensure_ascii=False))
    STORE.save(session)
    await _emit_run_start(websocket, session_id, "Problem confirmed. Collecting evidence and building the schema…")

    run_dir = RUNS_DIR / run_id

    def runner(emit) -> str:
        return run_segment_schema(problem, steps, paths, f"{run_id}:{AGENT_ID}:schema", run_dir, emit)

    async def on_done(sid: str, final: str) -> str | None:
        return await _finalize_schema_gate(websocket, sid, final)

    await _stream_run(websocket, session_id, runner, on_done)


async def handle_confirm_schema(websocket: Any, session_id: str, echo: bool = True) -> None:
    """The user confirmed (and may have edited via Schema Studio) the schema.
    Promote the draft, rebuild the workspace, and run segment 3 (extract + solve).

    ``echo`` controls the canned confirmation bubble (True for the explicit
    Confirm button; False when the caller already echoed the user's own words)."""
    session = STORE.get(session_id)
    run_id = session.get("run_id", "")
    if not run_id:
        return
    run_dir = RUNS_DIR / run_id
    clar = session.get("clarification") or {}
    problem = str(clar.get("problem") or "").strip()
    if not problem:
        # Fall back to the most recent user question if the gate state was lost.
        for message in reversed(session.get("messages", [])):
            if message.get("role") == "user" and message.get("content"):
                problem = str(message["content"]).strip()
                break
    set_stage(session, "confirm_schema", "done")
    paths = resolve_upload_paths(session_id, session, [])
    if echo:
        confirm_message = ui_message("user", "Confirmed the schema. Please continue solving.")
        session["messages"].append(confirm_message)
        await websocket.send_text(json.dumps({"type": "message", "message": confirm_message}, ensure_ascii=False))
    STORE.save(session)
    await _emit_run_start(websocket, session_id, "Schema confirmed. Extracting data and solving…")

    def runner(emit) -> str:
        # Promote the (possibly edited) draft schema to confirmed and rebuild the
        # workspace so extraction runs against exactly what the user approved.
        finalize_schema(run_dir)
        return run_segment_solve(problem, paths, f"{run_id}:{AGENT_ID}:solve", run_dir, emit)

    async def on_done(sid: str, final: str) -> str | None:
        return await _finalize_answer(websocket, sid, final)

    await _stream_run(websocket, session_id, runner, on_done)


async def handle_revise_problem(
    websocket: Any, session_id: str, instruction: str, echo: bool = True
) -> None:
    """The user asked to change the clarified problem/steps. Re-run Step 1 with
    the revision via problem_clarifier, then re-open the problem gate."""
    instruction = instruction.strip()
    if not instruction:
        return
    session = STORE.get(session_id)
    run_id = session.get("run_id", "")
    if not run_id:
        return
    clar = session.get("clarification") or {}
    prior_problem = str(clar.get("problem") or "").strip()
    prior_steps = [str(item) for item in (clar.get("steps") or [])]
    paths = resolve_upload_paths(session_id, session, [])
    if echo:
        user_message = ui_message("user", instruction)
        session["messages"].append(user_message)
        await websocket.send_text(json.dumps({"type": "message", "message": user_message}, ensure_ascii=False))
    STORE.save(session)
    await _emit_run_start(websocket, session_id, "Updating the problem statement per your request…")

    run_dir = RUNS_DIR / run_id

    def runner(emit) -> str:
        return run_segment_clarify_revise(
            prior_problem or instruction,
            prior_problem,
            prior_steps,
            instruction,
            paths,
            f"{run_id}:{AGENT_ID}:clarify-revise",
            run_dir,
            emit,
        )

    async def on_done(sid: str, final: str) -> str | None:
        return await _finalize_problem_gate(websocket, sid, final)

    await _stream_run(websocket, session_id, runner, on_done)


async def handle_revise_schema(
    websocket: Any, session_id: str, instruction: str, echo: bool = True
) -> None:
    """The user asked to change the schema. Patch it via schema_builder (PATCH
    mode) + re-judge, then re-open the schema gate for another review."""
    instruction = instruction.strip()
    if not instruction:
        return
    session = STORE.get(session_id)
    run_id = session.get("run_id", "")
    if not run_id:
        return
    clar = session.get("clarification") or {}
    problem = str(clar.get("problem") or "").strip()
    if not problem:
        for message in reversed(session.get("messages", [])):
            if message.get("role") == "user" and message.get("content"):
                problem = str(message["content"]).strip()
                break
    paths = resolve_upload_paths(session_id, session, [])
    if echo:
        user_message = ui_message("user", instruction)
        session["messages"].append(user_message)
        await websocket.send_text(json.dumps({"type": "message", "message": user_message}, ensure_ascii=False))
    STORE.save(session)
    await _emit_run_start(websocket, session_id, "Revising the schema per your request…")

    run_dir = RUNS_DIR / run_id

    def runner(emit) -> str:
        return run_segment_schema_revise(
            problem, instruction, paths, f"{run_id}:{AGENT_ID}:schema-revise", run_dir, emit
        )

    async def on_done(sid: str, final: str) -> str | None:
        return await _finalize_schema_gate(websocket, sid, final)

    await _stream_run(websocket, session_id, runner, on_done)


async def handle_general(
    websocket: Any, session_id: str, content: str, reopen_gate: str | None = None
) -> None:
    """Answer a general/non-pipeline message directly with a bare LLM call.

    Assumes the caller already appended the user's message to the transcript. If
    ``reopen_gate`` is set, the open gate's card is re-emitted afterwards so it
    stays actionable (the frontend only renders gate buttons on the last message)."""
    session = STORE.get(session_id)
    history = _recent_dialogue(session, limit=12)
    # Drop the trailing user turn (the current message) so it isn't duplicated.
    if history and history[-1][0] == "user" and history[-1][1] == content.strip():
        history = history[:-1]
    await _emit_run_start(websocket, session_id, "Answering directly…")

    def runner(emit) -> str:
        return run_general_answer(content, history, emit)

    async def on_done(sid: str, final: str) -> str | None:
        return await _finalize_general(websocket, sid, final, reopen_gate)

    await _stream_run(websocket, session_id, runner, on_done)


async def _finalize_general(
    websocket: Any, session_id: str, final: str, reopen_gate: str | None
) -> str | None:
    """Deliver a direct general answer, then re-open the gate card if one is open."""
    session = STORE.get(session_id)
    if not final:
        return "This run produced no reply. Please retry."
    message = ui_message("assistant", final)
    session["messages"].append(message)
    STORE.save(session)
    await _send_assistant_final(websocket, session, message)

    if reopen_gate == "confirm_problem":
        clar = session.get("clarification") or {}
        if clar.get("problem"):
            gate_message = ui_message(
                "assistant",
                "Back to the problem confirmation — confirm or edit the problem and steps to continue.",
                clarification={"problem": clar.get("problem"), "steps": clar.get("steps", [])},
                waiting="confirm_problem",
            )
            session["messages"].append(gate_message)
            STORE.save(session)
            await _send_assistant_final(websocket, session, gate_message)
    elif reopen_gate == "confirm_schema":
        gate_message = ui_message(
            "assistant",
            "Back to the schema confirmation — review and confirm it below; "
            "open Schema Studio to edit before confirming if needed.\n\n"
            "**Entity Definitions**\n\n**Relation Schema**",
            waiting="confirm_schema",
        )
        session["messages"].append(gate_message)
        STORE.save(session)
        await _send_assistant_final(websocket, session, gate_message)
    return None


async def _finalize_problem_gate(websocket: Any, session_id: str, final: str) -> str | None:
    """Open the problem-confirmation gate from segment 1's clarification reply."""
    session = STORE.get(session_id)
    clarification = extract_clarification(final)
    if not clarification:
        # The clarifier did not return parseable JSON; surface its text directly
        # rather than hanging, and leave the pipeline without a gate.
        if final:
            message = ui_message("assistant", final)
            session["messages"].append(message)
            STORE.save(session)
            await _send_assistant_final(websocket, session, message)
            return None
        return "This run produced no reply. Please retry."
    set_stage(session, "confirm_problem", "waiting")
    session["clarification"] = {**clarification, "status": "waiting"}
    message = ui_message(
        "assistant",
        "I've clarified the problem and drafted the solution steps. Confirm or edit them to continue.",
        clarification=clarification,
        waiting="confirm_problem",
    )
    session["messages"].append(message)
    STORE.save(session)
    await _send_assistant_final(websocket, session, message)
    return None


async def _finalize_schema_gate(websocket: Any, session_id: str, final: str) -> str | None:
    """Open the schema-confirmation gate after segment 2 builds the schema."""
    session = STORE.get(session_id)
    run_id = session.get("run_id", "")
    run_dir = RUNS_DIR / run_id if run_id else None
    has_schema = run_dir is not None and (
        (run_dir / "concepts" / "draft_schema.py").exists()
        or (run_dir / "concepts" / "confirmed_schema.py").exists()
    )
    if not has_schema:
        # No schema was produced; surface the coordinator's text instead of gating.
        if final:
            message = ui_message("assistant", final)
            session["messages"].append(message)
            STORE.save(session)
            await _send_assistant_final(websocket, session, message)
            return None
        return "The schema could not be built. Please retry."
    open_schema_gate(run_dir)
    set_stage(session, "confirm_schema", "waiting")
    # The markers below let the frontend render the editable schema-review card.
    message = ui_message(
        "assistant",
        "I've built the ontology schema from the evidence. Review and confirm it below; "
        "open Schema Studio to edit before confirming if needed.\n\n"
        "**Entity Definitions**\n\n**Relation Schema**",
        waiting="confirm_schema",
    )
    session["messages"].append(message)
    STORE.save(session)
    await _send_assistant_final(websocket, session, message)
    return None


async def _finalize_answer(websocket: Any, session_id: str, final: str) -> str | None:
    """Deliver segment 3's final answer."""
    session = STORE.get(session_id)
    if final:
        message = ui_message("assistant", final)
        session["messages"].append(message)
        STORE.save(session)
        await _send_assistant_final(websocket, session, message)
        return None
    return "This run produced no reply. Please retry."


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
        await handle_confirm_problem(
            sink,
            session["id"],
            str(payload.get("problem", "")),
            [str(item) for item in (payload.get("steps") or [])],
        )
    elif payload_type == "confirm_schema":
        await handle_confirm_schema(sink, session["id"])
    elif payload_type == "revise_schema":
        await handle_revise_schema(sink, session["id"], str(payload.get("instruction", "")))
    elif payload_type == "revise_problem":
        await handle_revise_problem(sink, session["id"], str(payload.get("instruction", "")))
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
