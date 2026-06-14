# otology_based_harness

Ontology-based agent harness for turning a user question plus local/web evidence into an ontology schema, extracted data, and a final solver answer.

## Setup

```bash
cd /Users/chunmao-zhang/Documents/Code/otology_harness
# Edit .env.example and fill in your own keys.
```

Required in `.env.example` for the default `harness.json`:

- `DEEPSEEK_API_KEY`: model API key for the coordinator and subagents.
- `SERPER_API_KEY`: optional but needed when the evidence collector uses web search.

The harness automatically loads `.env.example` next to `harness.json`; an optional local `.env` can override it, and exported shell variables still take highest precedence.

## Run the Web UI

```bash
PYTHONPATH=. python3 -m harness.web_ui
```

Open `http://127.0.0.1:8080` or `http://localhost:8080`.

The legacy command below is also redirected to the ontology UI when this checkout uses the ontology `harness.json`:

```bash
PYTHONPATH=. python3 frontend/app.py
```

## Run from CLI

```bash
PYTHONPATH=. python3 -m harness.ontology.pipeline \
  -q "你的问题" \
  -u test_data/ontology/company_notes.md
```

Generic harness entry:

```bash
PYTHONPATH=. python3 -m harness.run --message "你的任务"
```

## Main Paths

- `harness.json`: provider, agent, and tool configuration.
- `otology_agent_workspace/AGENT.md`: ontology coordinator workflow.
- `otology_agent_workspace/subagent_worksapce/*/AGENT.md`: subagent prompts.
- `harness/ontology/pipeline.py`: thin autonomous pipeline driver.
- `runs/ontology_workspace_runs/`: ontology workflow artifacts.
