# Code On Graph Agent

Project-specific frontend for the `deepagents_kbqa_general` agent.

```bash
PYTHONPATH=. python3 workspaces/deepagents_kbqa_general/frontend/app.py
```

Open `http://127.0.0.1:8093`.

`app.py` includes a hardcoded `SILICONFLOW_API_KEY` fallback for local launch;
an exported environment variable still takes precedence.

Features:

- KDR-style chat interface shared with the Otology frontend.
- Persistent sessions stored in `outputs/deepagents_kbqa_general/frontend_sessions/`.
- Upload TXT, JSON/JSONL, or Excel triples into one shared graph ledger; every upload receives a UUID and display name.
- Each triple, entity, and relation carries graph id/name metadata; the chat dropdown activates either the full ledger or a filtered single-graph runtime before tools run.
- Upload flow uses `workspaces/deepagents_kbqa_general/code/graph_runtime.py` plus `prepare.py` to build SQLite lookup tables and ChromaDB entity/predicate vector indexes.
- Graph Explorer and Graph Management panels for inspecting, selecting, uploading, and deleting graph datasets.
- Frontend-level WebSocket token streaming from LangGraph message chunks without modifying harness code.

Upload endpoint:

```bash
curl -F 'dataset_name=demo' -F 'file=@graph.txt' http://127.0.0.1:8093/api/graphs/imports
```

Accepted triple formats:

```text
TXT:   Alice|works_at|Acme
JSON:  {"triples":[{"subject":"Alice","relation":"works_at","object":"Acme"}]}
Excel: columns subject | relation | object
```
