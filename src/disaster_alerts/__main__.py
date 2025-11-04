# src/disaster_alerts/__main__.py
"""
Module entrypoint so `python -m disaster_alerts ...` works.

Delegates to cli.main(argv) and exits with its return code.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
