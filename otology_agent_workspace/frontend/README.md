# Ontology QA Agent Frontend

An ontology QA frontend matching the `frontend/` (KC-Agent) visual style, built around the 7-step `ontology_coordinator` workflow.

## Getting Started

```bash
# Real agent mode (requires deepagents dependencies and a model API key)
PYTHONPATH=. python3 otology_agent_workspace/frontend/app.py

# Local UI demo mode (no model needed; runs a deterministic pipeline walkthrough)
ONTOLOGY_UI_MOCK=1 PYTHONPATH=. python3 otology_agent_workspace/frontend/app.py
```

Default address: http://127.0.0.1:8095 (override with the `PORT` environment variable).

## Business Sidebar (opened via the bottom-right FAB)

- **Files & Evidence**: upload CSV / TXT / MD files and review the evidence manifest for the current run (uploads / web sources).
- **Schema Studio**: shows the draft / confirmed schema in a dual view ("entity + relation tables" plus Python code). Entity names, semantic types and relation names are editable; apply changes and confirm the schema in one click (the schema confirmation gate in the workflow).
- **Run & Results**: visual progress for the 8-step pipeline (including the two user confirmation gates), plus an extraction summary and answer data sources.

## Design Conventions

- Fully reuses the KC-Agent visual system (same `style.css` base, blue gradient accent, light/dark themes, Inter font).
- The chat never exposes raw tool-call JSON or internal paths — only friendly stage hints.
- Sessions persist in `outputs/ontology_coordinator/frontend_sessions/`; uploads are stored in `otology_agent_workspace/data/uploads/`.
