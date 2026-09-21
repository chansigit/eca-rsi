"""Compatibility entry point: the code lives in `ecarsi.ui.umapdata`, outside the
identity hash (eca-rsi#10). Kept so `python -m ecarsi.umapdata` still works -- the
deployed control-plane launchers and every doc use that spelling. Import the
real module directly; this file must stay trivial, because it is hashed."""
import sys

from .ui.umapdata import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
