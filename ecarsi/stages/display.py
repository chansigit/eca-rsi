"""Pool program: sync a run's display zone (#25); with `final`, also archive its whole work tree.
usage: python -m ecarsi.stages.display <packet.json>   packet: {root, dest, record, final}
Writes synced.json in the working directory."""
import argparse
from pathlib import Path

from ..warm_pool.state import read, save

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path)
    packet = read(parser.parse_args().packet)
    from ..display import sync
    result = dict(display=sync(packet["root"], packet["dest"], packet["record"]))
    if packet.get("final"):
        from ..archive import pack
        result["archive"] = pack(packet["root"], packet["record"]["work"])
    save(Path.cwd() / "synced.json", result)
