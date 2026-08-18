from __future__ import annotations

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def resource_path(group: str, name: str) -> Path:
    """Locate data files in an installed wheel or a source checkout."""
    packaged = PACKAGE_ROOT / "resources" / group / name
    if packaged.exists():
        return packaged
    source = REPOSITORY_ROOT / group / name
    if source.exists():
        return source
    raise FileNotFoundError(f"Missing packaged resource: {group}/{name}")
