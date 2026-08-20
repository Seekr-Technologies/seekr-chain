#!/usr/bin/env python3
"""Thin entrypoint for the controller pod. See controllerlib/__init__.py for details."""

import sys
from pathlib import Path

# Ensure the resources dir (this file's dir) is importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from controllerlib.watch import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
