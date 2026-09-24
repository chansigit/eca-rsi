"""Resident model-turn runners: one process per catalog model, many turns in flight in one event loop.

A model turn is an HTTP exchange with the provider plus a little JSON. As a pool task it paid a
process start (~3.5 s), a queue wait (median 32 s on 2026-09-23), eleven inodes per call, and it
crowded the smallest nodes (59 turns on an 8-core node). The bridge keeps deciding (dispatch.py):
when the chosen model's runner is alive it drops a marker in bridge/runner-queue/<key>/ instead of
submitting to the pool, and the runner performs the turn in bridge/turns/<turn id>/ with the same
started.json / result.json contract the pool executor writes, so the bridge settles both alike.
Provider settings are process environment (session.py and the SDK read them), hence one model per
runner; `runners` supervises one runner per catalog model and restarts any that exits.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..warm_pool.state import digest, read, save
from .dispatch import configure, model_key, perform, policy

MAX_CALLS = 2000  # a runner drains and exits after this many turns; the supervisor starts a fresh one


async def serve_runner(root, model, *, once=False, max_calls=MAX_CALLS):
    root, key = Path(root), model_key(model)
    settings = policy(read(root / "config.json"))
    configure(model, settings["response_timeout_seconds"])
    queue, beat = root / "runner-queue" / key, root / "runners" / (key + ".json")
    queue.mkdir(parents=True, exist_ok=True)
    beat.parent.mkdir(exist_ok=True)
    generation = digest([os.getpid(), time.time()])[:16]
    tasks, done, stopping = {}, 0, asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopping.set)
    log(f"runner {key} for {model['harness']}:{model['model']} started, pid {os.getpid()}")
    try:
        while True:
            settings = policy(read(root / "config.json"))  # model_concurrency is online
            for turn_id in [t for t, task in tasks.items() if task.done()]:
                task = tasks.pop(turn_id)
                done += 1
                if task.exception():
                    log(f"{turn_id}: {type(task.exception()).__name__}: {task.exception()}")
            draining = stopping.is_set() or done >= max_calls
            save(beat, dict(pid=os.getpid(), generation=generation, model=model, observed_at=time.time(),
                            in_flight=len(tasks), done=done, draining=draining))
            if not draining:
                for marker in sorted(queue.glob("*.json")):
                    if len(tasks) >= settings["model_concurrency"]:
                        break
                    if marker.stem not in tasks and (item := read(marker)):
                        tasks[marker.stem] = asyncio.create_task(run_one(marker, item))
            if not tasks and (draining or once and not any(queue.glob("*.json"))):
                break
            await asyncio.sleep(.5)
    finally:
        save(beat, dict(pid=os.getpid(), generation=generation, model=model, observed_at=time.time(),
                        in_flight=len(tasks), done=done, draining=True, stopped_at=time.time()))
        log(f"runner {key} stopped after {done} turns")
    return 0


async def run_one(marker, item):
    turn_dir = Path(item["turn_dir"])
    try:
        plan = read(item["plan"])
        if plan is None:
            raise FileNotFoundError(item["plan"])
        await perform(plan, turn_dir)
    except Exception as exc:  # noqa: BLE001 - the bridge reads result.json, never our stack
        if not (turn_dir / "result.json").exists():
            save(turn_dir / "result.json", dict(outcome="local_error", response=None, error=type(exc).__name__,
                                                 worker=dict(host=os.uname().nodename.split(".")[0], pid=os.getpid()),
                                                 elapsed_seconds=None, model=item.get("model"), provider_response=None))
        raise
    finally:
        marker.unlink(missing_ok=True)


def supervise(root, interval=5):
    """One runner per catalog model; restart any that exits (they drain after MAX_CALLS turns)."""
    from ..model_web import normalized_models
    from ..warm_pool.backend import parent_death_signal
    root = Path(root)
    children, stopping = {}, False

    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    (root / "runner-logs").mkdir(exist_ok=True)
    log(f"runner supervisor started for {root}")
    try:
        while not stopping:
            config = read(root / "config.json")
            wanted = {model_key(m): m for m in normalized_models(read(config["catalog"]))}
            for key, model in wanted.items():
                proc = children.get(key)
                if proc is not None and proc.poll() is None:
                    continue
                if proc is not None:
                    log(f"runner {key} exited with {proc.returncode}; restarting")
                with (root / "runner-logs" / (key + ".log")).open("ab") as stream:
                    children[key] = subprocess.Popen(
                        [sys.executable, "-m", "ecarsi.agent", "runner", str(root), "--model", json.dumps(model)],
                        stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, start_new_session=True,
                        preexec_fn=parent_death_signal(os.getpid()))
            for key in [k for k in children if k not in wanted]:
                children.pop(key).terminate()  # dropped from the catalog: finish in flight, then stop
            time.sleep(interval)
    finally:
        for proc in children.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in children.values():
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
        log("runner supervisor stopped")
    return 0


def log(message):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), message, flush=True)
