"""Python entry adapter for independently versioned MSP/ZMIP orchestration."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys

from ecarsi.run_state import write_json
from harness_bridge.control import pausable


@pausable
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[:2] not in (['-m', 'msp'], ['-m', 'zmip']):
        os.execv(sys.executable, [sys.executable, *argv])
    kernel = argv[1]
    namespace = __package__
    target = f'{namespace}.{kernel}.__main__'
    if '--outdir' in argv:
        out = Path(argv[argv.index('--outdir')+1])
        record = out/'.rsi-stage-runtime.json'
        root = Path(__file__).parent
        digest = hashlib.sha256()
        for path in sorted(root.rglob('*.py')):
            digest.update(path.relative_to(root).as_posix().encode()+path.read_bytes())
        identity = dict(module=target, code_sha256=digest.hexdigest())
        if record.exists():
            recorded = json.loads(record.read_text())
            previous = recorded.get('module', '').removesuffix(f'.{kernel}.__main__')
            if previous != namespace and previous.isidentifier() and (root.parent/previous/'stage_python.py').is_file():
                # The recorded adapter checks its own immutable code digest.
                # A new fleet default must not take over an existing stage.
                os.execv(sys.executable, [sys.executable, '-P', '-m', previous+'.stage_python', *argv])
            if recorded != identity:
                raise ValueError(f'orchestration changed for {out}; use its recorded runtime')
        elif any(out.rglob('*progress.json')):
            # Accepted decisions from the previous agent implementation remain
            # owned by that implementation, with its original source checks.
            target = kernel+'.__main__'
        else:
            write_json(record, identity)
    from .driver_budget import restore
    sys.argv = [argv[1], *argv[2:]]
    try:
        runpy.run_module(target, run_name='__main__')
    finally:
        restore()  # the unchanged RSI caller may next perform local heavy work


if __name__ == '__main__':
    raise SystemExit(main() or 0)
