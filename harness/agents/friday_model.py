"""FridayChatOpenAI — ChatOpenAI subclass that preserves reasoning_content.

Some OpenAI-compatible thinking models return `reasoning_content` in
choices[].message or streamed deltas, but:
1. langchain-openai's `_create_chat_result` doesn't parse it into the AIMessage
2. langchain-openai's `_convert_message_to_dict` doesn't serialize it back

Both must be fixed for multi-turn tool-call conversations to work against APIs
that require the thinking trace to be echoed back with assistant tool calls.

Fix:
- Override `_create_chat_result` to extract and store reasoning_content in
  AIMessage.additional_kwargs
- Override `_get_request_payload` to inject reasoning_content back into the
  messages array before sending to the API
"""

from __future__ import annotations

from typing import Any

import openai
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI


def _inject_reasoning_into_payload(messages_payload: list[dict]) -> list[dict]:
    """Add reasoning_content back into assistant messages that have it stored
    in additional_kwargs, so FRIDAY API receives it on subsequent turns."""
    result = []
    for msg in messages_payload:
        if msg.get("role") == "assistant":
            # Find the original langchain message to check additional_kwargs
            # We do this by checking if the message dict has extra fields needed
            pass
        result.append(msg)
    return result


class FridayChatOpenAI(ChatOpenAI):
    """ChatOpenAI with reasoning_content preservation for thinking models."""

    def _create_chat_result(
        self,
        response: dict | openai.BaseModel,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)

        # Extract reasoning_content from the raw response
        choices: list[Any] = []
        if isinstance(response, dict):
            choices = response.get("choices", [])
        elif hasattr(response, "choices") and response.choices:
            choices = response.choices

        for gen, choice in zip(result.generations, choices):
            reasoning = None
            if isinstance(choice, dict):
                reasoning = (choice.get("message") or {}).get("reasoning_content")
            elif hasattr(choice, "message"):
                msg = choice.message
                extra = getattr(msg, "__pydantic_extra__", None) or {}
                reasoning = extra.get("reasoning_content") or getattr(msg, "reasoning_content", None)

            if reasoning and hasattr(gen.message, "additional_kwargs"):
                gen.message.additional_kwargs["reasoning_content"] = reasoning

        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ):
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if generation_chunk is None:
            return None

        try:
            choices = (
                chunk.get("choices", [])
                or chunk.get("chunk", {}).get("choices", [])
            )
            delta = (choices[0] or {}).get("delta") if choices else {}
            reasoning = (delta or {}).get("reasoning_content")
            message = getattr(generation_chunk, "message", None)
            if reasoning and hasattr(message, "additional_kwargs"):
                existing = message.additional_kwargs.get("reasoning_content", "")
                message.additional_kwargs["reasoning_content"] = f"{existing}{reasoning}"
        except Exception:
            pass
        return generation_chunk

    def _get_request_payload(
        self,
        input_: list[BaseMessage],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # Fix: langchain-openai >= 0.3 renames max_tokens -> max_completion_tokens,
        # but Friday's API proxy does NOT accept max_completion_tokens and rejects
        # requests when both params appear simultaneously.
        # Solution: always keep only max_tokens, drop max_completion_tokens.
        if "max_completion_tokens" in payload and "max_tokens" in payload:
            del payload["max_completion_tokens"]
        elif "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")

        # Walk through the messages in the payload and inject reasoning_content
        # back for any assistant message that had it stored in additional_kwargs.
        # We match by position: payload["messages"] corresponds to input_ (after
        # system prompt injection by langchain), so we iterate both together.
        lc_messages = list(input_)
        api_messages: list[dict] = payload.get("messages", [])

        # Build a map from position of assistant messages to their lc counterpart
        lc_idx = 0
        for api_msg in api_messages:
            if lc_idx >= len(lc_messages):
                break
            lc_msg = lc_messages[lc_idx]
            lc_idx += 1

            if (
                api_msg.get("role") == "assistant"
                and isinstance(lc_msg, AIMessage)
                and "reasoning_content" in lc_msg.additional_kwargs
            ):
                api_msg["reasoning_content"] = lc_msg.additional_kwargs["reasoning_content"]

        return payload
