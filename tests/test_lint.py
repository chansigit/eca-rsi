"""The lint rule set of pyproject.toml [tool.ruff] holds for the whole repository (#48)."""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_ruff_check_is_clean():
    if importlib.util.find_spec("ruff"):
        command = [sys.executable, "-m", "ruff"]
    elif shutil.which("ruff"):
        command = ["ruff"]
    else:
        pytest.skip("ruff is not installed (ops/runsci.sh takes it from PYTEST_LIBS)")
    result = subprocess.run(command + ["check", "--no-cache", "--output-format", "concise", str(ROOT)],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
