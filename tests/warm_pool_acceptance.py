"""Real HQ fault test; run explicitly on reserved CPUs, never during pytest.

python tests/warm_pool_acceptance.py --hq /path/hq --directory /durable/test-root --cpus 0,1
"""
import argparse
from collections import Counter
from contextlib import nullcontext
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time

from ecarsi.warm_pool.__main__ import initialize
from ecarsi.warm_pool.backend import HyperQueue
from ecarsi.warm_pool.state import cancel, file_digest, read, status, submit
from ecarsi.warm_pool.worker import identity, reconcile_local


def eventually(fn, timeout=30):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return value
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            last = str(exc)
        time.sleep(.2)
    raise AssertionError("condition timed out: " + str(last))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--hq", required=True)
    p.add_argument("--directory", type=Path, required=True)
    p.add_argument("--cpus", required=True)
    a = p.parse_args()
    cpus = [int(v) for v in a.cpus.split(",")]
    assert len(cpus) == 2 and set(cpus) <= os.sched_getaffinity(0)
    a.directory.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="acceptance-", dir=a.directory))
    initialize(root, a.hq, sys.executable)
    backend = HyperQueue(root)
    processes = []
    def spawn(*args):
        with (root / f"launcher-{len(processes)}.log").open("a") as log:
            proc = subprocess.Popen([sys.executable, "-m", "ecarsi.warm_pool", "--root", str(root), *args],
                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        processes.append(proc)
        return proc
    def request(name, seconds=1, memory=64):
        code = ("from pathlib import Path;import time,json,os;"
                f"p=Path({str(root / 'starts.jsonl')!r});"
                f"f=p.open('a');f.write(json.dumps(dict(name={name!r},pid=os.getpid()))+'\\n');f.flush();os.fsync(f.fileno());f.close();"
                f"time.sleep({seconds});Path('result.json').write_text(json.dumps(dict(name={name!r})))")
        return dict(request_id=name, operation_id=name, args=["-c", code], cpus=1,
                    memory_mb=memory, timeout_seconds=30, outputs=["result.json"])
    def deliver(name):
        item = status(root, name)
        return subprocess.run([sys.executable, "-m", "ecarsi.warm_pool.worker", "execute",
                               str(root), name, item["attempt_id"]], timeout=5).returncode
    def descendants(name, child_seconds=60):
        child = f"import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep({child_seconds})"
        code = ("import subprocess,sys,signal,time,json;from pathlib import Path;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                f"p=subprocess.Popen([sys.executable,'-c',{child!r}]);"
                f"Path({str(root / (name + '-child.json'))!r}).write_text(json.dumps(dict(pid=p.pid)));"
                "print('started descendant',p.pid,flush=True);time.sleep(60)")
        return dict(request(name), args=["-c", code])
    def child_ready(name):
        data = read(root / (name + "-child.json"))
        return identity(data["pid"]) if data else None
    try:
        with nullcontext(tempfile.mkdtemp(prefix="rsi-hq-")) as local:
            scheduler = spawn("scheduler", "--host", "127.0.0.1")
            eventually(lambda: read(root / "scheduler.json", {}).get("state") == "running")
            original_server = backend.call("server", "info")
            workers = [spawn("worker", "--cpus", str(cpu), "--memory-mb", "192",
                             "--work-dir", str(Path(local) / str(i))) for i, cpu in enumerate(cpus)]
            eventually(lambda: len([w for w in backend.call("worker", "list") if w.get("ended") is None]) == 2)
            big = submit(root, request("too-large", memory=320))
            first = request("a", seconds=10)
            result = submit(root, first)
            assert submit(root, first)["attempt_id"] == result["attempt_id"]
            submit(root, request("b", seconds=10))
            eventually(lambda: status(root, "a")["accepted"] and status(root, "b")["accepted"])
            assert deliver("a") == 75  # actual duplicate transport delivery, not just repeated submission
            assert not status(root, "too-large")["accepted"]
            print("parallel computation and small-task backfill: PASS", flush=True)
            scheduler.kill()
            scheduler.wait(timeout=10)
            submit(root, request("offline-submission"))
            eventually(lambda: status(root, "a")["state"] == status(root, "b")["state"] == "succeeded")
            assert not status(root, "offline-submission")["accepted"]
            print("scheduler crash: computations finished, receipts saved, offline submission durable: PASS", flush=True)
            scheduler = spawn("scheduler", "--host", "127.0.0.1")
            eventually(lambda: read(root / "scheduler.json", {}).get("pid") == scheduler.pid)
            eventually(lambda: status(root, "offline-submission")["state"] == "succeeded")
            new_server = backend.call("server", "info")
            assert original_server["pid"] != new_server["pid"]
            counts = Counter(json.loads(line)["name"] for line in (root / "starts.jsonl").read_text().splitlines())
            assert counts == {"a": 1, "b": 1, "offline-submission": 1}, counts
            assert deliver("a") == 0  # persisted receipt makes redelivery a no-op
            cancel(root, "too-large")
            eventually(lambda: status(root, "too-large")["state"] == "cancelled")
            # Restore a server while the old executor is still computing.
            submit(root, request("overlap-restart", seconds=10))
            eventually(lambda: status(root, "overlap-restart")["usage"])
            scheduler.kill()
            scheduler.wait(timeout=10)
            scheduler = spawn("scheduler", "--host", "127.0.0.1")
            eventually(lambda: read(root / "scheduler.json", {}).get("pid") == scheduler.pid)
            assert not status(root, "overlap-restart")["receipt"]
            submit(root, request("after-early-restart"))
            eventually(lambda: status(root, "after-early-restart")["state"] == "succeeded")
            assert not status(root, "overlap-restart")["receipt"]
            eventually(lambda: status(root, "overlap-restart")["state"] == "succeeded")
            print("early scheduler restart: new work runs while old execution finishes, without duplicate compute: PASS", flush=True)

            submit(root, descendants("cancel-running"))
            child = eventually(lambda: child_ready("cancel-running"))
            time.sleep(.5)  # let the child install its SIGTERM handler
            cancel(root, "cancel-running")
            eventually(lambda: status(root, "cancel-running")["state"] == "cancelled")
            assert status(root, "cancel-running")["receipt"]["state"] != "succeeded"
            assert identity(child["pid"]) != child
            submit(root, dict(descendants("timeout"), timeout_seconds=3))
            child = eventually(lambda: child_ready("timeout"))
            eventually(lambda: status(root, "timeout")["receipt"])
            assert "TimeoutError" in status(root, "timeout")["receipt"]["error"]
            assert identity(child["pid"]) != child
            print("cancellation and timeout: signal-resistant child processes stopped before receipt: PASS", flush=True)

            for proc in workers:
                proc.terminate()
            for proc in workers:
                proc.wait(timeout=15)
            worker_dir = str(Path(local) / "combined")
            worker = spawn("worker", "--cpus", a.cpus, "--memory-mb", "384", "--work-dir", worker_dir)
            eventually(lambda: read(Path(worker_dir) / "worker.json"))
            submit(root, request("concurrent-a", seconds=6))
            submit(root, request("concurrent-b", seconds=6))
            eventually(lambda: status(root, "concurrent-a")["usage"] and status(root, "concurrent-b")["usage"])
            submit(root, request("bounded-third"))
            time.sleep(1)
            assert not status(root, "bounded-third")["accepted"]
            eventually(lambda: all(status(root, n)["state"] == "succeeded"
                                   for n in ("concurrent-a", "concurrent-b", "bounded-third")))
            print("one worker: two concurrent tasks, third task waits for CPU capacity: PASS", flush=True)

            submit(root, dict(descendants("worker-crash"), cpus=2))
            child = eventually(lambda: child_ready("worker-crash"))
            time.sleep(.5)
            worker.kill()
            worker.wait(timeout=10)
            spawn("worker", "--cpus", a.cpus, "--memory-mb", "384", "--work-dir", worker_dir)
            submit(root, request("after-worker-crash"))
            eventually(lambda: status(root, "after-worker-crash")["state"] == "succeeded")
            lost = status(root, "worker-crash")
            assert lost["receipt"]["state"] == "failed" and lost["receipt"]["retryable"]
            assert identity(child["pid"]) != child
            assert status(root, "after-worker-crash")["accepted"]["started_at"] >= lost["receipt"]["finished_at"]
            print("worker supervisor crash: descendant cleanup precedes CPU re-advertisement: PASS", flush=True)

            # The executor itself can die while HQ stays alive and frees its
            # grant. Its surviving process group still fences the next command.
            submit(root, dict(descendants("executor-crash", child_seconds=6), cpus=2))
            child = eventually(lambda: child_ready("executor-crash"))
            time.sleep(.5)
            os.kill(status(root, "executor-crash")["accepted"]["identity"]["pid"], signal.SIGKILL)
            submit(root, dict(request("after-executor-crash"), cpus=2))
            time.sleep(1)
            assert identity(child["pid"]) == child
            assert not status(root, "after-executor-crash")["accepted"]
            eventually(lambda: status(root, "after-executor-crash")["state"] == "succeeded")
            lost = status(root, "executor-crash")["receipt"]
            assert lost["state"] == "failed" and "WorkerLost" in lost["error"]
            assert status(root, "after-executor-crash")["accepted"]["started_at"] >= lost["finished_at"]
            print("executor SIGKILL: surviving descendants fence subsequent computation until stopped: PASS", flush=True)

            source = root / "input.txt"
            source.write_text("original")
            wrong_input = dict(request("changed-input"), inputs=[dict(path=str(source), sha256=file_digest(source))])
            source.write_text("changed after request construction")
            submit(root, wrong_input)
            eventually(lambda: status(root, "changed-input")["receipt"])
            assert "input identity mismatch" in status(root, "changed-input")["receipt"]["error"]
            assert not status(root, "changed-input")["accepted"]
            submit(root, dict(request("missing-output"), outputs=["missing.json"]))
            eventually(lambda: status(root, "missing-output")["receipt"])
            assert status(root, "missing-output")["state"] == "failed"
            print("changed input blocked before execution; missing output cannot become success: PASS", flush=True)
            submit(root, dict(request("memory-limit"), args=["-c", "import time;data=bytearray(96*1024*1024);time.sleep(20)"]))
            eventually(lambda: status(root, "memory-limit")["receipt"])
            assert "MemoryError" in status(root, "memory-limit")["receipt"]["error"]

            # Fault injection exactly between recording the child identity and
            # authorizing exec; run directly only after the HQ queue is drained.
            scheduler.terminate()
            scheduler.wait(timeout=15)
            gated = submit(root, dict(request("launch-gate"), cpus=2))
            crash_code = f"""import os,signal
from ecarsi.warm_pool import worker
save = worker.save
def crash(path, value):
    save(path, value)
    if path.name == 'accepted.json' and 'pgid' in value:
        os.kill(os.getpid(), signal.SIGKILL)
worker.save = crash
worker.execute({str(root)!r}, 'launch-gate', {gated['attempt_id']!r})
"""
            crash = subprocess.run([sys.executable, "-c", crash_code], timeout=10)
            assert crash.returncode == -signal.SIGKILL
            eventually(lambda: not reconcile_local(root, cpus) and status(root, "launch-gate")["receipt"])
            assert status(root, "launch-gate")["state"] == "failed"
            print("memory watchdog and crash before launch authorization: PASS", flush=True)

            successful = ("a", "b", "offline-submission", "overlap-restart", "after-early-restart",
                          "concurrent-a", "concurrent-b", "bounded-third", "after-worker-crash", "after-executor-crash")
            counts = Counter(json.loads(line)["name"] for line in (root / "starts.jsonl").read_text().splitlines())
            assert counts == dict.fromkeys((*successful, "missing-output"), 1), counts
            for name in successful:
                receipt = status(root, name)["receipt"]
                output = receipt["outputs"][0]
                assert file_digest(output["path"]) == output["sha256"] and output["size"] > 0
            report = dict(root=str(root), passed=True, version="0.26.2", starts=dict(counts),
                          old_server_pid=original_server["pid"], new_server_pid=new_server["pid"],
                          tests=["bounded_parallelism", "small_task_backfill", "idempotent_submission",
                                 "finish_without_scheduler", "durable_offline_submission", "automatic_worker_rejoin",
                                 "receipt_before_journal_replay", "cancel_queued", "cancel_running",
                                 "duplicate_transport_delivery", "restart_during_execution", "timeout_descendants",
                                 "single_worker_concurrency", "worker_crash_cleanup", "executor_crash_fencing",
                                 "input_identity", "required_outputs", "memory_watchdog", "durable_launch_gate"])
            (root / "acceptance.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report), flush=True)
    finally:
        for proc in reversed(processes):
            if proc.poll() is None:
                proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        for log in Path(local).glob("*/worker.log"):
            shutil.copy2(log, root / ("worker-" + log.parent.name + ".log"))
        shutil.rmtree(local)


if __name__ == "__main__":
    main()
