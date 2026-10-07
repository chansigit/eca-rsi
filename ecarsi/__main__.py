"""eca-rsi -- the package's command line. The pipeline itself runs on the control plane
(container/control-plane.sh; `python -m ecarsi.control ... start-dataset`); these commands show its results.

    eca-rsi serve     [dir...] [--registry F] [--port] [--ngrok --domain D] [--auth U:P | --auth-file F]
    eca-rsi serve     scan-add|remove|list|dump|reload ...   (edit the dataset list)
    eca-rsi index     <root|unit>          re-render a run's pages
    eca-rsi umapdata  <h5ad> <out.json>    the plotting data of a release UMAP
    eca-rsi top       --root <run dir>     the control-plane monitor in the terminal (ops/rtop)

`python -m ecarsi ...` is the same thing.
"""

from __future__ import annotations

import importlib
import sys

COMMANDS = {"serve": "ecarsi.ui.serve", "index": "ecarsi.ui.index", "umapdata": "ecarsi.ui.umapdata",
            "top": "ecarsi.ui.top"}


def main(argv: list[str] | None = None) -> int:
    from harness_bridge import configure_logging
    configure_logging("ecarsi", stream=sys.stderr)
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] not in COMMANDS:
        print(f"unknown command {argv[0]!r}; expected one of {', '.join(COMMANDS)}")
        return 2
    return importlib.import_module(COMMANDS[argv[0]]).main(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
