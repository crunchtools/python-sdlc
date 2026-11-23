"""Puts the repository root on sys.path, so a suite can import `sdlc_jobs` by name.

Not a suite itself (no `test_` prefix). A suite imports it before anything from
`sdlc_jobs`, which keeps every import at the top of the file.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
