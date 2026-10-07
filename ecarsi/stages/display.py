"""Pool program: sync a run's display zone (#25); with `final`, also archive its whole work tree. The run's
`published` sync first freezes its hard agent steps (decision 0021), before the pruner may delete their evidence.
usage: python -m ecarsi.stages.display <packet.json>   packet: {root, dest, record, final, label}
Writes synced.json in the working directory."""
import argparse
from pathlib import Path

from ..files import read, save

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path)
    packet = read(parser.parse_args().packet)
    result = {}
    if packet.get("label") == "published":
        from .cases import freeze, library
        try:
            result["cases"] = freeze(packet["root"], library(packet["record"]))
        except Exception as exc:  # a lost case library is not worth the run (decision 0013)
            from .. import degraded
            degraded.save(packet["root"], [degraded.note("hard-case freeze", exc)], stage="published")
    from ..display import sync
    result["display"] = sync(packet["root"], packet["dest"], packet["record"])
    if packet.get("final"):
        from .archive import pack
        result["archive"] = pack(packet["root"], packet["record"]["work"])
    save(Path.cwd() / "synced.json", result)
