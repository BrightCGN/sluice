"""Mistral-AI-Adapter — Chat-Completions-Dialekt (Spec §7.3)."""

from __future__ import annotations

from sluice.providers.openai_compat import OpenAICompatAdapter


class MistralAdapter(OpenAICompatAdapter):
    name = "mistral"
    key_env = "SLUICE_MISTRAL_API_KEY"
    default_base_url = "https://api.mistral.ai/v1"
