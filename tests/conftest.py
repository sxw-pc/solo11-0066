"""Pytest bootstrap: keep test imports local and the suite fully offline."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
