"""OpenAI-Adapter — Chat-Completions-Dialekt (Spec §7.3)."""

from __future__ import annotations

from sluice.providers.openai_compat import OpenAICompatAdapter


class OpenAIAdapter(OpenAICompatAdapter):
    name = "openai"
    key_env = "SLUICE_OPENAI_API_KEY"
    default_base_url = "https://api.openai.com/v1"
