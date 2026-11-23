# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
"""Cinema4D-compatible command entry point for the ComfyUI installer builder."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_installer import main

if __name__ == "__main__":
    sys.exit(main())
