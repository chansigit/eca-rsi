"""Retained-driver entry: protect released units without changing kernels."""
import os
from pathlib import Path
import runpy
import sys


def main():
    argv = sys.argv[1:]
    if argv[:1] == ['-P']:
        argv = argv[1:]
    if argv[:2] != ['-m', 'ecarsi']:
        os.execv(sys.executable, [sys.executable, *argv])
    from ecarsi import persample
    from .osp_dispatch import released
    original = persample.main
    def guarded(arguments):
        if arguments and not arguments[0].startswith('-'):
            unit = Path(arguments[0]).resolve()
            if unit.is_dir() and released(unit):
                return 0
        return original(arguments)
    persample.main = guarded
    sys.argv = ['ecarsi', *argv[2:]]
    runpy.run_module('ecarsi', run_name='__main__')


if __name__ == '__main__':
    main()
