"""Configure AstrBot's provider through its existing configuration and manager."""

import json
from pathlib import Path

from astrbot.api.provider import ProviderType
from astrbot.api.star import Context

from .provider import PROVIDER_ID, PROVIDER_TYPE


async def configure_provider(context: Context, config: dict, data_dir: Path) -> None:
    """Install the opt-in local provider and per-sender group sessions.

    Args:
        context: Live AstrBot plugin context.
        config: Bridge configuration, including the local CLI path.
        data_dir: Private directory for the previous setting values.
    """
    if not config.get("integrate_plugins", False):
        return
    global_config = context.get_config()
    runner = global_config["agent_runner"]
    if runner["runner_type"] != "local":
        raise ValueError("Local Codex requires AstrBot's local Agent Runner.")
    provider_config = {
        "id": PROVIDER_ID,
        "type": PROVIDER_TYPE,
        "provider_type": "chat_completion",
        "enable": True,
        "model": str(config.get("model", "")).strip(),
        "codex_exe": str(config.get("codex_exe", "")),
        "timeout_seconds": int(config.get("timeout_seconds", 180)),
        "fast_mode": bool(config.get("fast_mode", True)),
        "reasoning_effort": str(config.get("reasoning_effort", "low")),
        "max_parallel_requests": int(config.get("max_parallel_requests", 2)),
        "modalities": ["text", "image", "tool_use"],
    }
    providers = global_config["provider"]
    existing = next((item for item in providers if item["id"] == PROVIDER_ID), None)
    sources = global_config.get("provider_sources", [])
    source_id = existing.get("provider_source_id") if existing else None
    source = next((item for item in sources if item["id"] == source_id), None)
    if source_id and source is None:
        raise ValueError("The local_codex provider source is missing.")
    effective_config = {**(source or {}), **(existing or {})}
    if existing and effective_config.get("type") != PROVIDER_TYPE:
        raise ValueError("Another provider already uses the local_codex ID.")
    backup = data_dir / "settings-before-provider.json"
    if not backup.exists():
        backup.write_text(
            json.dumps(
                {
                    "provider_id": runner["config"]["model"]["provider_id"],
                    "unique_session": global_config["platform_settings"][
                        "unique_session"
                    ],
                    "show_tool_use_status": global_config["provider_settings"].get(
                        "show_tool_use_status", True
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    if existing is None:
        providers.append(provider_config)
        existing = provider_config
    elif source is not None:
        # AstrBot migrates backend settings into a separate provider source.
        # Keep that layout so startup and later dashboard saves use the same values.
        for key, value in provider_config.items():
            if key in {"id", "enable", "model", "modalities"}:
                existing[key] = value
            else:
                source[key] = value
                existing.pop(key, None)
    else:
        existing.update(provider_config)
    runner["config"]["model"]["provider_id"] = PROVIDER_ID
    global_config["platform_settings"]["unique_session"] = True
    global_config["provider_settings"]["show_tool_use_status"] = False
    await global_config.save_config_async()
    manager = context.provider_manager
    manager.providers_config = providers
    manager.provider_sources_config = sources
    manager.default_chat_provider_id = PROVIDER_ID
    # Startup loads providers after plugins. A completed/active MCP-init task
    # distinguishes a hot reload, where the provider manager is already running.
    if manager._mcp_init_task is not None:
        await manager.reload(existing)
        await manager.set_provider(PROVIDER_ID, ProviderType.CHAT_COMPLETION)
