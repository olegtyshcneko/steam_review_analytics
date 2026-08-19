"""Deprecated compatibility namespace; use :mod:`games_analytics`."""

from __future__ import annotations

import importlib
import sys

from games_analytics import __version__

_MODULES = (
    "analysis_jobs",
    "batch_worker",
    "cli",
    "config",
    "contracts",
    "database",
    "domain",
    "llm",
    "mcp_server",
    "openrouter_batch",
    "pipeline",
    "resources",
    "taxonomy",
)

for _name in _MODULES:
    sys.modules[f"{__name__}.{_name}"] = importlib.import_module(f"games_analytics.{_name}")
