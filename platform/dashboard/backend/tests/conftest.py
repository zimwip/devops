"""conftest.py — shared pytest fixtures."""

import sys
from pathlib import Path

# Make sure scripts/ and backend/ are importable during tests
REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "dashboard" / "backend"))
