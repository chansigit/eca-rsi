"""An agent's lookup tool call is a fresh process; its imports are most of its cost (batch 2: median 3.6 s per call).
A file lookup (read_evidence, list_evidence, sample_inventory, type_context) imports no numerics, and a DEG lookup
(deg_lookup, deg_sql) only the standard library (msp.deg_tables; deg_sql's formatting adds pandas): the kernel packages
and msp.evidence load scanpy and matplotlib where they are used, not at import."""
import subprocess
import sys

HEAVY = ("scanpy", "anndata", "sklearn", "numba", "matplotlib", "zarr")

CODE = f"""
import sys
import ecarsi.stages.crosssample, ecarsi.stages.persample, ecarsi.stages.zoomin
print(sorted(m for m in ("numpy", "pandas", *{HEAVY!r}) if m in sys.modules))
from msp.api import DegTables
from zmip.api import TYPE_KEY, QUALITY_KEY
print(sorted(m for m in ("numpy", "pandas", "scipy") if m in sys.modules))
from msp.api import DegCache, load_removal_mask
print(sorted(m for m in {HEAVY!r} if m in sys.modules))
"""


def test_lookup_tool_calls_import_no_scanpy_anndata_or_matplotlib():
    run = subprocess.run([sys.executable, "-c", CODE], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout.split("\n")[:3] == ["[]", "[]", "[]"]
