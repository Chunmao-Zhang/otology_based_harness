"""DeepSeek ChatOpenAI wrapper that preserves reasoning_content.

DeepSeek thinking-mode responses can include ``reasoning_content`` on assistant
messages. In multi-turn/tool-call conversations, DeepSeek requires that value to
be sent back with the matching assistant message. langchain-openai currently
drops unknown provider fields during parse/serialization, so this wrapper stores
the field on AIMessage.additional_kwargs and injects it back into later payloads.
"""

from __future__ import annotations

from typing import Any

import openai
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI


def _get_mapping_value(value: Any, key: str) -> Any:
    """Read an OpenAI SDK object or dict without depending on SDK internals."""

    if isinstance(value, dict):
        return value.get(key)
    if hasattr(value, key):
        return getattr(value, key)
    extra = getattr(value, "__pydantic_extra__", None) or getattr(value, "model_extra", None) or {}
    if isinstance(extra, dict):
        return extra.get(key)
    return None


def _choice_message(choice: Any) -> Any:
    return _get_mapping_value(choice, "message") or _get_mapping_value(choice, "delta") or {}


def _reasoning_from_choice(choice: Any) -> str | None:
    reasoning = _get_mapping_value(_choice_message(choice), "reasoning_content")
    return reasoning if isinstance(reasoning, str) and reasoning else None


def _reasoning_from_message(message: AIMessage) -> str | None:
    reasoning = message.additional_kwargs.get("reasoning_content")
    if not reasoning:
        reasoning = message.response_metadata.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) and reasoning else None


class DeepSeekChatOpenAI(ChatOpenAI):
    """ChatOpenAI with DeepSeek thinking-mode reasoning preservation."""

    def _create_chat_result(
        self,
        response: dict | openai.BaseModel,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)

        choices = []
        if isinstance(response, dict):
            choices = response.get("choices", []) or []
        elif hasattr(response, "choices"):
            choices = getattr(response, "choices") or []

        for generation, choice in zip(result.generations, choices):
            reasoning = _reasoning_from_choice(choice)
            if reasoning and hasattr(generation.message, "additional_kwargs"):
                generation.message.additional_kwargs["reasoning_content"] = reasoning

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

        choices = chunk.get("choices", []) or chunk.get("chunk", {}).get("choices", [])
        if choices:
            reasoning = _reasoning_from_choice(choices[0])
            message = getattr(generation_chunk, "message", None)
            if reasoning and hasattr(message, "additional_kwargs"):
                message.additional_kwargs["reasoning_content"] = reasoning

        return generation_chunk

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        langchain_messages = self._convert_input(input_).to_messages()
        for api_message, langchain_message in zip(payload.get("messages", []), langchain_messages):
            if api_message.get("role") != "assistant" or not isinstance(langchain_message, AIMessage):
                continue
            reasoning = _reasoning_from_message(langchain_message)
            if reasoning:
                api_message["reasoning_content"] = reasoning

        return payload
