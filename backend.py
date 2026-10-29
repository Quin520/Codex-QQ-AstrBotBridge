"""Resolve the chat backend independently from QQ identity and memory features."""

from .provider import PROVIDER_ID


def uses_api(config: dict) -> bool:
    """Keep existing installations on CLI until API mode is explicitly selected."""
    return config.get("model_backend", "cli") == "api"


def provider_id(config: dict) -> str:
    """Return the configured model without allowing API mode to fall back to CLI."""
    if not uses_api(config):
        return PROVIDER_ID
    selected = str(config.get("api_provider_id", "")).strip()
    if not selected or selected == PROVIDER_ID:
        raise ValueError("Choose an enabled API chat provider in the bridge settings.")
    return selected


def uses_pipeline(config: dict) -> bool:
    """API requests always retain AstrBot's hooks, conversations, and tools."""
    return uses_api(config) or bool(config.get("integrate_plugins", False))
