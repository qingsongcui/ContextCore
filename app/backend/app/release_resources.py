"""Explicit product inputs shared by source snapshots and native packaging.

Runtime databases, logs, drafts and account documents are never resources merely
because they happen to live next to Python modules.
"""
from pathlib import Path


RESOURCE_FILES = (
    "app/services/semantic-runtime-requirements.txt",
    "app/services/semantic-runtime-wheels.json",
    "app/integrations/host_capability_evidence.json",
    "app/integrations/omp_extension.ts",
    "app/integrations/opencode_plugin.ts",
    "app/integrations/skills/claude_code_skill.md",
    "app/integrations/skills/codex_skill.md",
    "app/integrations/skills/antigravity_skill.md",
    "app/integrations/skills/cursor_skill.md",
    "app/integrations/skills/opencode_command.md",
    "app/integrations/skills/omp_command.md",
)
CODE_DIRECTORIES = ("app", "app/adapters", "app/integrations", "app/routers", "app/services")
RAW_CODE_RESOURCES = ("app/integrations/cc_hook.py",)


def source_files(backend: Path) -> list[Path]:
    result = [path for directory in CODE_DIRECTORIES
              for path in (backend / directory).glob("*.py")
              if not path.name.startswith(".") and path.is_file()]
    result += [backend / relative for relative in RESOURCE_FILES if (backend / relative).is_file()]
    return sorted(result)


def native_data_files(backend: Path) -> list[tuple[str, str]]:
    files = []
    for relative in (*RESOURCE_FILES, *RAW_CODE_RESOURCES):
        path = backend / relative
        if (not path.is_file() or path.is_symlink()
                or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(backend))):
            raise ValueError(f"Required packaged resource is missing or unsafe: {relative}")
        files.append((str(path), str(Path(relative).parent)))
    return files
