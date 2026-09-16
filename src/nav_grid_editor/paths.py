"""Package and source-checkout paths."""

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parents[1]
CONFIG_DIR = PROJECT_ROOT / "configs"

__all__ = ["CONFIG_DIR", "PACKAGE_DIR", "PROJECT_ROOT"]
