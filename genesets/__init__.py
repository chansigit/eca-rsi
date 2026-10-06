"""Gene sets the kernels share (decision 0020): mitochondrially encoded genes and the dissociation-stress panel.

Both match a dataset's gene names case-insensitively and need no species: one rule serves human, mouse, rat,
rhesus, cynomolgus, marmoset, mouse lemur and zebrafish. stress_panel.tsv records every gene considered for the
panel with its evidence and the reason it is in or out."""
import csv
from functools import cache
from pathlib import Path

# The rule of stangene.mito (github.com/chansigit/stangene): an MT- prefix (human MT-CO1, mouse mt-Co1, rat Mt-co1;
# fruit fly mt:CoI), or a bare name where the reference has no prefix (rhesus, cynomolgus and mouse lemur ND1, COX1;
# C. elegans nduo-1, ctc-1). No bare name is an official symbol of human, mouse, rat or zebrafish; COX1 and COX2
# are human aliases of PTGS1 and PTGS2, which standardized data names by their symbols.
MITO_PREFIXES = ("MT-", "MT:")
MITO_BARE = frozenset({"ND1", "ND2", "ND3", "ND4", "ND4L", "ND5", "ND6", "COX1", "COX2", "COX3", "CYTB", "ATP6", "ATP8",
                       "ATP-6", "CTB-1", "CTC-1", "CTC-2", "CTC-3", "NDUO-1", "NDUO-2", "NDUO-3", "NDUO-4", "NDUO-5",
                       "NDUO-6"})
PANEL_FILE = Path(__file__).with_name("stress_panel.tsv")


def is_mito(name) -> bool:
    name = str(name).upper()
    return name.startswith(MITO_PREFIXES) or name in MITO_BARE


@cache
def _panel() -> tuple[tuple[str, ...], frozenset[str]]:
    with open(PANEL_FILE) as f:
        rows = [r for r in csv.DictReader((line for line in f if not line.startswith("#")), delimiter="\t")
                if r["decision"] == "panel"]
    return tuple(r["gene"] for r in rows), frozenset(n.upper() for r in rows for n in r["match"].split("|"))


def stress_panel() -> tuple[str, ...]:
    """The panel's genes by their human symbols."""
    return _panel()[0]


def is_stress(name) -> bool:
    """Whether a gene name or Ensembl ID is a panel gene in any species of the table."""
    return str(name).upper() in _panel()[1]
