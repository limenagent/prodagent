"""LLM provider resolution.

The framework does not ship a built-in vendor list. Three paths, picked by env:

1. ``USE_FAKE_LLM=1`` — offline FakeLLM, no key.
2. ``LLM_BASE_URL`` + ``LLM_API_KEY`` + ``LLM_MODEL`` — any OpenAI-compatible
   endpoint (DeepSeek, Qwen, Moonshot, Zhipu, Groq, Ollama, self-hosted
   gateways, ...). The user owns the endpoint; the framework doesn't.
3. ``ANTHROPIC_API_KEY`` (+ optional ``ANTHROPIC_MODEL`` / ``ANTHROPIC_BASE_URL``)
   — native Anthropic SDK path.

Priority: Fake > OpenAI-compatible > Anthropic > Fake fallback.
"""

from __future__ import annotations

import os

_DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"


def use_fake_llm() -> bool:
    return os.getenv("USE_FAKE_LLM", "").lower() in ("1", "true", "yes")


def openai_compat_env() -> tuple[str, str, str] | None:
    """Return ``(base_url, api_key, model)`` if LLM_BASE_URL is set."""
    base_url = os.getenv("LLM_BASE_URL", "").strip()
    if not base_url:
        return None
    api_key = os.getenv("LLM_API_KEY", "").strip() or "dummy"
    model = os.getenv("LLM_MODEL", "").strip() or "gpt-4o"
    return base_url, api_key, model


def anthropic_env() -> tuple[str, str | None, str | None] | None:
    """Return ``(api_key, model, base_url)`` if ANTHROPIC_API_KEY is set."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.getenv("ANTHROPIC_MODEL", "").strip() or None
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip() or None
    return api_key, model, base_url


def detect_default_model() -> str:
    """Used by ``LLMConfig.__post_init__`` when model is left blank."""
    if use_fake_llm():
        return "fake-llm"
    compat = openai_compat_env()
    if compat:
        return compat[2]
    anthropic = anthropic_env()
    if anthropic:
        return anthropic[1] or _DEFAULT_ANTHROPIC_MODEL
    return _DEFAULT_ANTHROPIC_MODEL
