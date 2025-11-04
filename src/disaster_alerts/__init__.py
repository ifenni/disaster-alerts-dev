# src/disaster_alerts/__init__.py
"""
disaster-alerts package init.

Exports
-------
__version__ : str
run()       : convenience wrapper to execute one pipeline run with default settings
"""

from __future__ import annotations

from .settings import Settings
from . import pipeline as _pipeline

# Bump this when you tag releases; used by CLI and User-Agent.
__version__ = "0.1.0"


def run() -> int:
    """
    Convenience runner:
        from disaster_alerts import run
        run()
    Equivalent to: Settings.load() → pipeline.run(settings)
    """
    settings = Settings.load()
    return _pipeline.run(settings)


__all__ = ["__version__", "run"]
