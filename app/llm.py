"""OpenRouter LLM client (OpenAI-compatible) with tool-calling support."""
from __future__ import annotations

from typing import Any

import openai
from openai import OpenAI

from .config import Settings
from .logger import get_logger

log = get_logger("llm")


def make_client(settings: Settings) -> OpenAI:
    return OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=settings.openrouter_api_key,
        default_headers={
            "HTTP-Referer": "https://render.com",
            "X-Title": "personal-coding-agent",
        },
        timeout=120,
    )


def chat(
    client: OpenAI,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
) -> Any:
    """Blocking chat completion. Returns the raw message object."""
    log.debug("LLM call: model=%s msgs=%d", model, len(messages))
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    try:
        resp = client.chat.completions.create(**kwargs)
    except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError) as err:
        log.warning("LLM transient error: model=%s msgs=%d err=%s", model, len(messages), err)
        raise RuntimeError(f"LLM transient error for model={model}") from err
    except (openai.APIStatusError, openai.AuthenticationError, openai.BadRequestError) as err:
        log.error("LLM request failed: model=%s msgs=%d err=%s", model, len(messages), err)
        raise RuntimeError(f"LLM request failed for model={model}") from err
    if not resp.choices:
        raise RuntimeError(f"LLM returned no choices (model={model})")
    message = resp.choices[0].message
    if message is None:
        raise RuntimeError(f"LLM returned a null message (model={model})")
    return message
