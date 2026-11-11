"""Runtime version from the selected source snapshot or installed distribution."""
from importlib.metadata import version
from pathlib import Path
import sys
import tomllib


def package_version() -> str:
    project = Path(__file__).resolve().parents[1] / 'pyproject.toml'
    if not getattr(sys, 'frozen', False) and project.is_file():
        return tomllib.loads(project.read_text())['project']['version']
    return version('contextcore-backend')
