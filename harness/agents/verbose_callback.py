"""Verbose Callback Handler for SubAgent events.

Captures LLM calls and tool calls from subagents and forwards them
to a user-provided callback function for real-time terminal output.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult


class SubAgentCallbackHandler(BaseCallbackHandler):
    """Callback handler that captures subagent LLM and tool events.

    Events are forwarded to the on_event callback with signature:
        on_event(event_type: str, data: dict)

    event_type can be:
        - "llm_start": LLM call started (data: {"agent": ..., "messages": ...})
        - "llm_end": LLM call finished (data: {"agent": ..., "content": ..., "tool_calls": ...})
        - "tool_start": Tool call started (data: {"agent": ..., "tool": ..., "input": ...})
        - "tool_end": Tool call finished (data: {"agent": ..., "tool": ..., "output": ...})
    """

    def __init__(self, on_event):
        super().__init__()
        self.on_event = on_event
        # Track nesting depth: depth 0 = main agent, depth >= 1 = subagent
        self._depth = 0
        self._run_to_depth: dict[UUID, int] = {}

    def _is_subagent(self, run_id: UUID, parent_run_id: UUID | None) -> bool:
        """Determine if this run belongs to a subagent."""
        if parent_run_id and parent_run_id in self._run_to_depth:
            depth = self._run_to_depth[parent_run_id] + 1
        else:
            depth = 0
        self._run_to_depth[run_id] = depth
        return depth >= 1

    def _get_agent_tag(self, tags: list[str] | None) -> str:
        """Extract agent name from tags if available."""
        if tags:
            for tag in tags:
                if tag.startswith("graph:"):
                    return tag.replace("graph:", "")
        return "subagent"

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if self._is_subagent(run_id, parent_run_id):
            agent_name = self._get_agent_tag(tags)
            # Extract last user message for context
            last_msg = ""
            if messages and messages[0]:
                for m in reversed(messages[0]):
                    if getattr(m, "type", "") == "human":
                        last_msg = getattr(m, "content", "")[:200]
                        break
            self.on_event("llm_start", {
                "agent": agent_name,
                "message_count": len(messages[0]) if messages else 0,
            })

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if run_id in self._run_to_depth and self._run_to_depth[run_id] >= 1:
            agent_name = self._get_agent_tag(tags)
            # Extract content and tool_calls from the response
            content = ""
            tool_calls = []
            if response.generations and response.generations[0]:
                gen = response.generations[0][0]
                msg = getattr(gen, "message", None)
                if msg:
                    content = getattr(msg, "content", "") or ""
                    tc = getattr(msg, "tool_calls", None)
                    if tc:
                        tool_calls = [
                            {"name": c.get("name", ""), "args": c.get("args", {})}
                            for c in tc
                        ]
            self.on_event("llm_end", {
                "agent": agent_name,
                "content": content,
                "tool_calls": tool_calls,
            })

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if self._is_subagent(run_id, parent_run_id):
            agent_name = self._get_agent_tag(tags)
            tool_name = serialized.get("name", "unknown")
            self.on_event("tool_start", {
                "agent": agent_name,
                "tool": tool_name,
                "input": input_str[:300],
            })

    def on_tool_end(
        self,
        output: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if run_id in self._run_to_depth and self._run_to_depth[run_id] >= 1:
            agent_name = self._get_agent_tag(tags)
            self.on_event("tool_end", {
                "agent": agent_name,
                "output": str(output)[:500],
            })
