"""Supervise an existing service command; never request or release Slurm jobs."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import time

from .pool.slurm import exited, stop_worker
from ecarsi.run_state import write_json, writer_lock


def supervise(command, state_path, stopping):
    state = dict(pid=os.getpid(), command=command, restarts=0)
    child = None
    delay = 1
    retry_at = 0
    started = 0
    with writer_lock(state_path.with_suffix('.lock')):
        try:
            while not stopping():
                now = time.monotonic()
                if child is not None and exited(child):
                    stop_worker(child)
                    state['last_exit_code'] = child.returncode
                    child = None
                    delay = 1 if now-started >= 300 else min(60, delay*2)
                    retry_at = now+delay
                    state['restarts'] += 1
                if child is None and now >= retry_at:
                    try:
                        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
                        started = now
                    except OSError as exc:
                        state['last_error'] = str(exc)
                        retry_at = now+delay
                        delay = min(60, delay*2)
                state.update(state='running' if child else 'restarting', child_pid=child.pid if child else None,
                             updated_at=time.time())
                try:
                    write_json(state_path, state)
                except OSError as exc:
                    print(f"[service] health record unavailable: {exc}", flush=True)
                time.sleep(1)
        finally:
            if child is not None:
                stop_worker(child)
            state.update(state='stopped', child_pid=None, updated_at=time.time())
            write_json(state_path, state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a service command is required')
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    supervise(command, args.state, lambda: stopping)


if __name__ == '__main__':
    main()
