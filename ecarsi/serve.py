"""Compatibility entry point: the code lives in `ecarsi.ui.serve`. Kept so `python -m ecarsi.serve`
still works -- launchers and docs use that spelling. Import the real module directly."""
import sys

from .ui.serve import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
