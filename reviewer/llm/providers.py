"""Choose the model provider. Each provider brings its own model names."""

from __future__ import annotations

from collections.abc import Callable

from reviewer.llm.gateway import LLM
from reviewer.llm.ollama import OllamaGateway
from reviewer.settings import Settings

PROVIDERS = ("anthropic", "ollama")


def build_llm(
    settings: Settings,
    provider: str | None = None,
    anthropic_factory: Callable[[], LLM] | None = None,
) -> tuple[LLM, Settings]:
    """Return the client and the settings to use with it.

    For Ollama the stage models are replaced with the local ones from `settings.ollama.models`.
    `anthropic_factory` exists so callers (and tests) control how the Claude client is built.
    """
    name = provider or settings.provider
    if name == "anthropic":
        if anthropic_factory is None:
            from reviewer.llm.gateway import AnthropicGateway

            anthropic_factory = AnthropicGateway
        return anthropic_factory(), settings
    if name == "ollama":
        local = settings.model_copy(deep=True)
        local.models = settings.ollama.models.model_copy()
        client = OllamaGateway(host=settings.ollama.host, num_ctx=settings.ollama.num_ctx)
        return client, local
    raise ValueError(f"unknown provider {name!r}; choose from {', '.join(PROVIDERS)}")
