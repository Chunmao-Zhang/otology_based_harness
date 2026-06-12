"""Contract validation for problem_clarifier output."""

from __future__ import annotations

import json
from typing import Any

from harness.ontology.json_contract import extract_json_object


FORBIDDEN_STEP_TERMS = {
    "web_search",
    "source_reader",
    "schema_builder",
    "schema_judger",
    "data_extractor",
    "problem_clarifier",
}


def validate_problem_clarifier_output(text: str) -> dict[str, Any]:
    """Validate and normalize the problem_clarifier JSON contract."""
    try:
        data = extract_json_object(text)
    except Exception as exc:
        return {
            "ok": False,
            "errors": [f"output is not a parseable JSON object: {exc}"],
            "repair_instruction": _repair_instruction(),
        }

    errors: list[str] = []
    allowed_keys = {"problem", "steps"}
    extra_keys = sorted(set(data) - allowed_keys)
    missing_keys = sorted(allowed_keys - set(data))
    if extra_keys:
        errors.append(f"extra keys are not allowed: {extra_keys}")
    if missing_keys:
        errors.append(f"missing required keys: {missing_keys}")

    problem = data.get("problem")
    steps = data.get("steps")
    if not isinstance(problem, str) or not problem.strip():
        errors.append("problem must be a non-empty string")
    if not isinstance(steps, list) or not steps:
        errors.append("steps must be a non-empty list")
    elif not all(isinstance(item, str) and item.strip() for item in steps):
        errors.append("every step must be a non-empty string")
    else:
        joined_steps = "\n".join(steps)
        forbidden = sorted(term for term in FORBIDDEN_STEP_TERMS if term in joined_steps)
        if forbidden:
            errors.append(f"steps must not mention tool or subagent names: {forbidden}")

    if errors:
        return {
            "ok": False,
            "errors": errors,
            "repair_instruction": _repair_instruction(),
        }

    return {
        "ok": True,
        "problem": problem.strip(),
        "steps": [item.strip() for item in steps],
    }


def _repair_instruction() -> str:
    return (
        "Return exactly one JSON object with only these keys: "
        '{"problem": "<non-empty string>", "steps": ["<non-empty string>", "..."]}. '
        "No markdown, no fenced code block, no extra text, no extra keys."
    )


def validate_problem_clarifier_output_json(text: str) -> str:
    """Return the validation result as JSON for local harness checks."""
    return json.dumps(validate_problem_clarifier_output(text), ensure_ascii=False)
