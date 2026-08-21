from __future__ import annotations

import logging
from dataclasses import replace

from prodagent.llm.base import LLMClient, LLMConfig
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.llm.providers import anthropic_env, openai_compat_env, use_fake_llm

logger = logging.getLogger(__name__)


def create_llm_client(
    config: LLMConfig | None = None,
    *,
    force_fake: bool = False,
) -> LLMClient:
    # A library does not touch global logging configuration — if httpx/openai
    # chatter bothers you, quiet those loggers in your own logging setup.

    if force_fake or use_fake_llm():
        logger.info("LLM: FakeLLMAdapter (USE_FAKE_LLM or force_fake)")
        return FakeLLMAdapter()

    compat = openai_compat_env()
    if compat is not None:
        base_url, api_key, model = compat
        cfg = config or LLMConfig()
        resolved_model = config.model if config and config.model else model
        if cfg.model != resolved_model:
            cfg = replace(cfg, model=resolved_model)
        from prodagent.llm.openai_adapter import OpenAIAdapter

        logger.info("LLM: OpenAIAdapter (base_url=%s, model=%s)", base_url, cfg.model)
        return OpenAIAdapter(api_key=api_key, base_url=base_url, default_config=cfg)

    anthropic = anthropic_env()
    if anthropic is not None:
        api_key, anthro_model, anthro_base_url = anthropic
        cfg = config or LLMConfig()
        resolved_model = (
            config.model
            if config and config.model
            else (anthro_model or (cfg.model if cfg.model else ""))
        )
        if cfg.model != resolved_model:
            cfg = replace(cfg, model=resolved_model)
        from prodagent.llm.anthropic_adapter import AnthropicAdapter

        logger.info(
            "LLM: AnthropicAdapter (model=%s, base_url=%s)", cfg.model, anthro_base_url or "default"
        )
        return AnthropicAdapter(api_key=api_key, default_config=cfg, base_url=anthro_base_url)

    logger.info("LLM: FakeLLMAdapter (no provider env var set)")
    return FakeLLMAdapter()
