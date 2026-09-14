"""Opt-in SSH/Apptainer acceptance on two already budgeted Slurm hosts.

The plan explicitly assigns one worker CPU and one control CPU per host.
This test never allocates nodes or takes resources away from another pool.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import time

from ecarsi.warm_pool.backend import HyperQueue
from ecarsi.warm_pool.state import cancel, file_digest, read, save, status, submit


def eventually(fn, timeout=60):
    until = time.monotonic() + timeout
    last = None
    while time.monotonic() < until:
        try:
            result = fn()
            if result:
                return result
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            last = str(exc)
        time.sleep(.5)
    raise AssertionError("timed out: " + str(last))


def ssh(host, script):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                           "/usr/bin/python3 -c " + shlex.quote(script)],
                          capture_output=True, text=True, timeout=35, check=True)


def run(plan, root):
    code = str(Path(__file__).resolve().parents[1])
    prefix = ["env", "APPTAINERENV_PYTHONPATH=" + code, "APPTAINERENV_PYTHONDONTWRITEBYTECODE=1",
              "APPTAINERENV_OPENBLAS_NUM_THREADS=1", "apptainer", "exec", "--bind", "/scratch,/oak,/home", plan["image"]]
    cli = prefix + ["/usr/local/bin/python3", "-m", "ecarsi.warm_pool", "--root", str(root)]
    subprocess.run(cli + ["init", "--hq", plan["hq"]], check=True, capture_output=True, timeout=30)
    backend = HyperQueue(root)
    backend.command = prefix + backend.command  # client calls only; servers run native HQ inside their own container
    nodes = plan["nodes"]
    assert len(nodes) == 2 and nodes[0]["host"] != nodes[1]["host"]
    actors = []

    def launch(node, role):
        index = len(actors)
        directory = root / (role + "-" + str(index))
        directory.mkdir()
        args = (["scheduler", "--host", node["host"]] if role == "scheduler" else
                ["worker", "--cpus", str(node["worker_cpu"]), "--memory-mb", "192", "--work-dir", str(directory)])
        cpu = node["control_cpu"] if role == "scheduler" else node["worker_cpu"]
        command = ["taskset", "-c", str(cpu)] + cli + args
        actor = dict(node=node, role=role, directory=directory)
        actors.append(actor)
        script = f"""import subprocess,json,os
from pathlib import Path
log=Path({str(directory / 'launcher.log')!r}).open('a')
p=subprocess.Popen({command!r},stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
Path({str(directory / 'launcher.json')!r}).write_text(json.dumps(dict(pid=p.pid)))
"""
        ssh(node["host"], script)
        def ready():
            info = read(root / "scheduler.json", {}) if role == "scheduler" else read(directory / "worker.json", {})
            if role == "scheduler" and (info.get("host", "").split(".")[0] != node["host"] or info.get("state") != "running"):
                return None
            if info.get("pid"):
                actor["pid"] = info["pid"]
                return info
        eventually(ready)
        return actor

    def stop(actor, sig=signal.SIGTERM):
        if actor.get("pid"):
            ssh(actor["node"]["host"], f"""import os,signal
from pathlib import Path
pid={actor['pid']}
try:
    args=Path('/proc/%d/cmdline'%pid).read_bytes().split(bytes([0]))
    if {str(root).encode()!r} in args and b'ecarsi.warm_pool' in args:
        os.kill(pid,{int(sig)})
except FileNotFoundError:
    pass
""")

    def request(name, seconds):
        task = f"""import hashlib,json,os,socket,time,uuid
from pathlib import Path
root=Path({str(root)!r})
host=socket.gethostname().split('.')[0]
start=dict(name={name!r},host=host,pid=os.getpid(),cpus=sorted(os.sched_getaffinity(0)))
(root/'starts'/({name!r}+'-'+uuid.uuid4().hex+'.json')).write_text(json.dumps(start))
data=(root/'input.bin').read_bytes()
deadline=time.monotonic()+{seconds}
cpu=time.process_time()
iterations=0
while time.monotonic()<deadline:
    result=hashlib.sha256(data).hexdigest()
    iterations+=1
start.update(sha256=result,iterations=iterations,cpu_seconds=time.process_time()-cpu,
             cgroup=Path('/proc/self/cgroup').read_text())
Path('result.json').write_text(json.dumps(start))
"""
        return dict(request_id=name, operation_id=name, args=["-c", task], cpus=1, memory_mb=64,
                    timeout_seconds=90, inputs=[dict(path=str(root / "input.bin"), sha256=file_digest(root / "input.bin"))],
                    outputs=["result.json"])

    (root / "starts").mkdir()
    (root / "input.bin").write_bytes(b"cross-node-input\n" * 65536)
    save(root / "plan.json", plan)
    try:
        for node in nodes:
            check = f"""import os,json,re,socket
from pathlib import Path
assert socket.gethostname().split('.')[0]=={node['host']!r}
assert {set((node['worker_cpu'], node['control_cpu']))!r} <= os.sched_getaffinity(0)
assert '/job_{node['job_id']}/' in Path('/proc/self/cgroup').read_text()
Path({str(root / (node['host'] + '-allocation.json'))!r}).write_text(json.dumps(dict(
host=socket.gethostname(),affinity=sorted(os.sched_getaffinity(0)),cgroup=Path('/proc/self/cgroup').read_text())))
"""
            ssh(node["host"], check)
        first = launch(nodes[0], "scheduler")
        for node in nodes:
            launch(node, "worker")
        eventually(lambda: len([w for w in backend.call("worker", "list") if w.get("ended") is None]) == 2)
        print("two Slurm hosts registered: PASS", flush=True)
        for name in ("a", "b"):
            submit(root, request(name, 25))
        eventually(lambda: all(status(root, n)["usage"] for n in ("a", "b")))
        hosts = {status(root, n)["accepted"]["host"] for n in ("a", "b")}
        assert hosts == {n["host"] for n in nodes}, hosts
        # A second host must fail to acquire the live scheduler's shared lock.
        command = prefix + ["/usr/local/bin/python3", "-c",
                  "from ecarsi.warm_pool.state import lock;from pathlib import Path;\n"
                  "try:\n with lock(Path(" + repr(str(root / "scheduler.lock")) + "),blocking=False): pass\n"
                  "except BlockingIOError: raise SystemExit(73)\n"]
        ssh(nodes[1]["host"], f"import subprocess;assert subprocess.call({command!r})==73")
        stop(first, signal.SIGKILL)
        submit(root, request("submitted-offline", 3))
        eventually(lambda: all(status(root, n)["state"] == "succeeded" for n in ("a", "b")))
        assert not status(root, "submitted-offline")["accepted"]
        print("cross-host execution and filesystem lock; scheduler loss preserves numerical work: PASS", flush=True)
        second = launch(nodes[1], "scheduler")
        eventually(lambda: status(root, "submitted-offline")["state"] == "succeeded")
        for name in ("after-move-a", "after-move-b"):
            submit(root, request(name, 5))
        names = ("a", "b", "submitted-offline", "after-move-a", "after-move-b")
        eventually(lambda: all(status(root, n)["state"] == "succeeded" for n in names))
        starts = [read(p) for p in (root / "starts").glob("*.json")]
        assert sorted(s["name"] for s in starts) == sorted(names), starts
        outputs = {}
        for name in names:
            receipt = status(root, name)["receipt"]
            output = receipt["outputs"][0]
            assert file_digest(output["path"]) == output["sha256"]
            value = read(output["path"])
            assert value["sha256"] == file_digest(root / "input.bin") and value["cpu_seconds"] > .1
            node = next(n for n in nodes if n["host"] == value["host"])
            assert value["cpus"] == [node["worker_cpu"]] and '/job_' + str(node['job_id']) + '/' in value['cgroup']
            outputs[name] = value
        report = dict(passed=True, nodes=nodes, scheduler_moved_to=second["node"]["host"], outputs=outputs,
                      tests=["two_slurm_hosts", "cpu_grants", "shared_input_output", "cross_host_flock",
                             "finish_without_scheduler", "scheduler_host_migration", "worker_reconnection", "no_duplicate_execution"])
        save(root / "acceptance.json", report)
        print("scheduler moved to the other host; workers rejoined; all five tasks executed once: PASS", flush=True)
        return report
    finally:
        for item in status(root):
            if not item["receipt"]:
                cancel(root, item["request_id"])
        for actor in reversed(actors):
            stop(actor)
        # Native supervisors finish cleanup before their container launchers exit.
        for actor in actors:
            launcher = read(actor["directory"] / "launcher.json")
            if launcher:
                ssh(actor["node"]["host"], f"""import time
from pathlib import Path
path=Path('/proc/{launcher['pid']}/stat')
for _ in range(100):
    if not path.exists() or path.read_text().rsplit(')',1)[1].split()[0]=='Z': break
    time.sleep(.2)
else: raise RuntimeError('test container has not exited')
""")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(read(args.plan), args.root)))
