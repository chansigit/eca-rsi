"""Route versioned orchestration logs through the ordinary CLI handlers."""
import logging
import os
import sys
import threading

_cleanup_registered = False


def close_stopped_event_loops():
    # PauseRequested (SystemExit) can stop asyncio.gather before AnyIO's root
    # task callback runs. Its idle, non-daemon workers otherwise block Python
    # shutdown forever. Queue normal shutdown only after their loop is closed;
    # join still lets any outstanding synchronous operation finish normally.
    backend = sys.modules.get('anyio._backends._asyncio')
    if backend is None:
        return
    for thread in threading.enumerate():
        if (isinstance(thread, backend.WorkerThread) and thread.loop.is_closed()
                and thread.root_task.done() and not thread.stopping):
            thread.stop()
            thread.join()
            print('[runtime] closed AnyIO thread after event-loop shutdown', file=sys.stderr, flush=True)


def install():
    global _cleanup_registered
    # This startup hook also runs in lineage subprocesses. It changes logging
    # only; retained orchestration modules and their checksums remain untouched.
    for namespace in os.environ.get('ECA_STAGE_LOG_NAMESPACES', '').split(':'):
        if not namespace or not namespace.isidentifier():
            continue
        for family in ('msp', 'zmip'):
            logging.getLogger(namespace+'.'+family).parent = logging.getLogger(family)
    if os.environ.get('ECA_STAGE_LOG_NAMESPACES'):
        logging.getLogger('__main__').parent = logging.getLogger('msp')
        if not _cleanup_registered:
            # Must run before threading's join, not ordinary atexit (too late).
            threading._register_atexit(close_stopped_event_loops)
            _cleanup_registered = True
