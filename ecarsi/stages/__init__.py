"""Stage programs: what the warm pool runs inside the science image.

organize / persample / crosssample / zoomin / release wrap the kernels (osp, msp, zmip) and
validate every model proposal on the host side; crosssample_v3 / zoomin_v3 only change what the
model sees (protocol v4) and delegate every computation to the v2 program underneath.
The control layer pins the program files it submits by content hash (`program`)."""
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
PROMPTS = PACKAGE / "prompts"


def program(name):
    """Source file of a stage program, for pinning into a pool request."""
    return Path(__file__).with_name(name + ".py")
