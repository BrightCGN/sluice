"""OpenAI-Adapter — Chat-Completions-Dialekt (Spec §7.3)."""

from __future__ import annotations

from sluice.providers.openai_compat import OpenAICompatAdapter


class OpenAIAdapter(OpenAICompatAdapter):
    name = "openai"
    key_env = "SLUICE_OPENAI_API_KEY"
    default_base_url = "https://api.openai.com/v1"
    # Rev. 16 (§7.6): OpenAI liefert den Stream-Verbrauch nur, wenn man ihn anfordert.
    # Ohne dieses Opt-in bliebe die Telemetrie im Streaming-Pfad dauerhaft leer.
    stream_usage_option = True
