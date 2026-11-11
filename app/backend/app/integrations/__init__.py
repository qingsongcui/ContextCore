# ContextCore Integrations Package
# Contains host hook definitions, OMP extension, skill/command templates, and setup bundle generators.

from app.integrations.constants import (
    SUPPORTED_HOSTS,
    HOST_CAPABILITY_STATUS,
    DEFAULT_BASE_URL,
)
from app.integrations.templates import (
    render_mcp_config,
    render_hooks_config,
    render_skill_or_command,
    render_setup_preview,
)
from app.integrations.installer import (
    generate_setup_bundle,
    SetupBundle,
)

__all__ = [
    "SUPPORTED_HOSTS",
    "HOST_CAPABILITY_STATUS",
    "DEFAULT_BASE_URL",
    "render_mcp_config",
    "render_hooks_config",
    "render_skill_or_command",
    "render_setup_preview",
    "generate_setup_bundle",
    "SetupBundle",
]
