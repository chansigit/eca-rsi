from datetime import datetime, timezone
import math

import pytest

from ecarsi.warm_pool.backend import cpu_capable, gpu_jobfile, hq_priority, hq_version, release_plan, unpin_due, worker_capacity


def c(key, klass="work", t=0, gpu=False):
    return dict(key=key, klass=klass, submitted_at=t, gpu_preferred=gpu)


KW = dict(gpu_queue_seconds=0, gpu_slots=0)


def bare(**release):
    """A scheduler object without a pool: the release knobs given here count as configured."""
    from ecarsi.warm_pool.backend import HyperQueue, RELEASE_DEFAULTS
    hq = HyperQueue.__new__(HyperQueue)
    hq.release, hq.overrides, hq.measured = dict(RELEASE_DEFAULTS, **release), set(release), {}
    return hq


def test_every_candidate_is_released_interactive_first():
    cands = [c("deg-new", t=5), c("deg-old", t=1), c("tool", "tool", t=9), c("agent", "agent", t=9)]
    plan = release_plan(cands, **KW)
    assert [k for k, _ in plan] == ["agent", "tool", "deg-old", "deg-new"]   # nothing is held back: HQ ranks them


def test_gpu_preferred_pinned_to_gpu_only_while_its_queue_is_short():
    cands = [c(f"compute-{i}", t=i, gpu=True) for i in range(6)]   # unmeasured: the 120 s / 360 s knobs
    plan = release_plan(cands, gpu_queue_seconds=120, gpu_slots=1)
    # one run ahead on one card: the first would end at 240 s < 360 s on cores, GPU-only; the second at 360 s, either
    assert [g for _, g in plan] == [True, False, False, False, False, False]
    plan = release_plan(cands, gpu_queue_seconds=120, gpu_slots=4)
    assert all(g for _, g in plan)  # four cards: every one ends sooner than a CPU run
    # measured per operation (zoom-in.compute: 44 s on the card, 81 s on cores): the card's queue in seconds decides
    measured = [dict(c(f"z-{i}", t=i, gpu=True), gpu_seconds=44, cpu_seconds=81) for i in range(3)]
    plan = release_plan(measured, gpu_queue_seconds=30, gpu_slots=1)
    assert [g for _, g in plan] == [True, False, False]   # 30 + 44 < 81; then 74 + 44 is not


def test_worker_capacity_reads_hq_json():
    w = {"ended": None, "configuration": {"resources": {"resources": [
        {"kind": "list", "name": "cpus", "values": ["0", "1", "2", "3"]},
        {"kind": "sum", "name": "mem", "size": 294910000},
        {"kind": "list", "name": "gpuSlot/0", "values": ["uuid#0"]}]}}}
    assert worker_capacity([w, dict(w, ended="x")]) == [(4, 29491.0, 1, math.inf)]
    timed = dict(w, started="2026-10-02T06:33:17.785409871Z", configuration=dict(w["configuration"], time_limit=7979.0))
    started = datetime(2026, 10, 2, 6, 33, 17, 785409, tzinfo=timezone.utc).timestamp()
    assert worker_capacity([timed], now=started + 1565)[0][3] == pytest.approx(6414)


def test_a_vanished_request_is_skipped_not_submitted(tmp_path):
    hq = bare()
    worker = {"configuration": {"resources": {"resources": [{"kind": "list", "name": "cpus", "values": list(range(48))}]}}}
    jobs = [{"task_stats": {"running": 1, "waiting": 0}}] * 10
    gone = dict(c("x"), folder=tmp_path, attempt=tmp_path, request={})   # request.json vanished: skipped
    assert hq._release([gone], jobs, lambda: [worker], "g") == []
    assert hq.release_state["skipped"] == {"gone": 1} and hq.release_state["candidates"] == 1


def test_release_knobs_follow_config_edits(tmp_path):
    import json, os
    from ecarsi.warm_pool.backend import HyperQueue, RELEASE_DEFAULTS
    hq = HyperQueue.__new__(HyperQueue)
    hq.root, hq.release, hq._release_stamp = tmp_path, dict(RELEASE_DEFAULTS), None
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"release": {"gpu_host_fraction": 0.3}}))
    hq._reload_release(); assert hq.release["gpu_host_fraction"] == 0.3
    config.write_text(json.dumps({"release": {"gpu_host_fraction": 0.2, "typo": 1}})); os.utime(config, ns=(1, 1))
    hq._reload_release(); assert hq.release["gpu_host_fraction"] == 0.3   # unknown knob: keep what is in force
    config.write_text(json.dumps({"release": {"pin_wait_seconds": 60}})); os.utime(config, ns=(2, 2))
    hq._reload_release(); assert hq.release["pin_wait_seconds"] == 60 and hq.release["gpu_host_fraction"] == 0.5


def test_scheduler_uses_a_separate_hq_server_only_while_one_holds_the_lock(tmp_path):
    from ecarsi.warm_pool.backend import external_hq_server
    from ecarsi.files import lock
    assert not external_hq_server(tmp_path)
    with lock(tmp_path / "hq-server.lock"):
        import subprocess, sys
        # another process (the hq-server component) holds it: the scheduler must not start its own
        probe = subprocess.run([sys.executable, "-c", "import sys; from pathlib import Path; "
                                "from ecarsi.warm_pool.backend import external_hq_server; "
                                f"sys.exit(0 if external_hq_server(Path({str(tmp_path)!r})) else 1)"])
        assert probe.returncode == 0
    assert not external_hq_server(tmp_path)


def test_numba_cache_rotates_once_an_index_outgrows_the_limit(tmp_path):
    from ecarsi.warm_pool.worker import NUMBA_INDEX_LIMIT, numba_cache
    first = numba_cache(tmp_path / "rsi-numba" / "digest")
    assert first.name == "gen-0" and numba_cache(tmp_path / "rsi-numba" / "digest") == first
    (first / "get_x").mkdir()
    (first / "get_x" / "_kernels.agg_sum_csr-15.py312.nbi").write_bytes(b"x" * (NUMBA_INDEX_LIMIT + 1))
    second = numba_cache(tmp_path / "rsi-numba" / "digest")
    assert second.name == "gen-1" and second.is_dir() and not any(second.iterdir())
    assert numba_cache(tmp_path / "rsi-numba" / "digest") == second
    root = tmp_path / "rsi-numba" / "digest"
    for n in range(2, 6):          # three more rotations: gen-0 and gen-1 fall out, the newest three stay
        full = root / f"gen-{n - 1}" / "get_x"
        full.mkdir(exist_ok=True)
        (full / "k.nbi").write_bytes(b"x" * (NUMBA_INDEX_LIMIT + 1))
        numba_cache(root)
    assert sorted(p.name for p in root.iterdir()) == ["gen-3", "gen-4", "gen-5"]


def test_numba_site_names_fast_array_utils_types_so_pickle_finds_them(tmp_path):
    import subprocess, sys
    from pathlib import Path
    import ecarsi.warm_pool.worker as worker
    plugin = tmp_path / "fast_array_utils" / "_plugins"
    plugin.mkdir(parents=True)
    (tmp_path / "fast_array_utils" / "__init__.py").write_text("")
    (plugin / "__init__.py").write_text("")
    (plugin / "numba_sparse.py").write_text("class Base: pass\nTYPES = [type('csr_matrixType', (Base,), {})]\nMODELS = {}\n")
    site = Path(worker.__file__).with_name("numba_site")
    probe = ("import pickle, sys; assert 'fast_array_utils' not in sys.modules; "
             "from fast_array_utils._plugins import numba_sparse as m; t = m.TYPES[0]; "
             "assert pickle.loads(pickle.dumps(t)) is t")
    run = subprocess.run([sys.executable, "-c", probe], env={"PYTHONPATH": f"{site}:{tmp_path}"}, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_a_task_pinned_to_the_gpu_cannot_use_a_drain(tmp_path):
    def jobfile(mode):
        request = dict(spec=dict(request_id='r', cpus=2, memory_mb=12288, time_request_seconds=600,
                                 gpu=dict(memory_mb=4096, mode=mode)), attempt_id='a', runtime_digest='d')
        return gpu_jobfile(request, tmp_path / 'requests' / 'r' / 'a', 'rsi.r', '/usr/bin/python3', '')
    pinned, either = tmp_path / 'pinned.toml', tmp_path / 'either.toml'
    pinned.write_text(jobfile('required'))   # what a gpu_only release writes: GPU variants only
    either.write_text(jobfile('preferred'))  # GPU variants plus the plain CPU variant
    assert not cpu_capable(pinned) and cpu_capable(either)
    assert cpu_capable(tmp_path / 'absent.toml')  # a CPU task has no job file


def test_a_pinned_task_is_unpinned_without_a_card_or_after_the_usual_wait():
    assert unpin_due(gpu_slots=0, queued_seconds=1, wait_seconds=600)     # the card's allocation ended
    assert not unpin_due(gpu_slots=1, queued_seconds=599, wait_seconds=600)
    assert unpin_due(gpu_slots=1, queued_seconds=601, wait_seconds=600)   # the short-queue bet was wrong


def test_an_unpinned_task_is_not_pinned_again():
    cands = [dict(key="g", klass="work", submitted_at=1, gpu_preferred=True, unpinned=True),
             dict(key="h", klass="work", submitted_at=2, gpu_preferred=True)]
    plan = release_plan(cands, gpu_queue_seconds=0, gpu_slots=1)
    assert plan == [("g", False), ("h", True)]  # an idle card still pins a fresh task, never the unpinned one


def test_a_pinned_task_is_resubmitted_with_the_cpu_variant_once_no_card_is_live(tmp_path, monkeypatch):
    import time
    from ecarsi.warm_pool.backend import HyperQueue, observe
    from ecarsi.files import read, save
    from ecarsi.warm_pool.state import observation
    (tmp_path / "requests").mkdir()
    save(tmp_path / "config.json", dict(hq="/bin/false", executor="/usr/bin/python3", runtime={}))
    spec = dict(request_id="r", operation_id="zoom-in.compute", cpus=2, memory_mb=12288, time_request_seconds=600,
                gpu=dict(memory_mb=4096, mode="preferred"), inputs=[])
    request = dict(spec=spec, attempt_id="a", runtime_digest="d", digest="x", submitted_at=1.0)
    folder, attempt = tmp_path / "requests" / "r", tmp_path / "requests" / "r" / "a"
    attempt.mkdir(parents=True)
    pinned = dict(request, spec=dict(spec, gpu=dict(spec["gpu"], mode="required")))  # what a gpu_only release wrote
    (attempt / "job.toml").write_text(gpu_jobfile(pinned, attempt, "rsi.r.a", "/usr/bin/python3", ""))
    assert not cpu_capable(attempt / "job.toml")
    save(folder / "request.json", request)
    observe(folder, request, dict(state="queued", job_id=1, generation="g", observed_at=time.time() - 30))
    calls = []

    def call(self, *args):
        calls.append(args)
        if args[:2] == ("job", "list"):
            return [dict(id=1, name="rsi.r.a", task_stats=dict(waiting=1, running=0, canceled=0, failed=0, finished=0, aborted=0))]
        if args[:2] == ("worker", "list"):
            return [{"configuration": {"resources": {"resources": [   # the card's allocation ended; a CPU node remains
                {"kind": "list", "name": "cpus", "values": list(range(8))}, {"kind": "sum", "name": "mem", "size": 32768 * 10000}]}}}]
        if args[:2] == ("job", "submit-file"):
            return dict(id=2)
        return None
    monkeypatch.setattr(HyperQueue, "call", call)
    HyperQueue(tmp_path).dispatch(dict(server_uid="s", pid=1, start_date="t"))
    assert ("job", "cancel", "1") in calls
    assert cpu_capable(attempt / "job.toml")  # resubmitted with the plain CPU variant beside the GPU ones
    record = observation(folder, read(folder / "request.json"))
    assert record["state"] == "queued" and record["job_id"] == 2


def test_hq_priority_is_class_then_cpus_and_can_be_switched_off(tmp_path):
    spec = dict(request_id='r', operation_id='zoom-in.compute', cpus=4, memory_mb=1024, time_request_seconds=60,
                gpu=dict(memory_mb=4096, mode='preferred'))
    assert hq_priority(spec, {'hq_priority': True}) == 40 and hq_priority(spec, {}) == 40   # batch work: 10 per CPU
    assert hq_priority(dict(spec, request_id='s.tool-1', cpus=1), {}) == 810                  # a session tool outranks any batch task
    assert hq_priority(dict(spec, operation_id='agent.call', cpus=1), {}) == 1010             # a model turn outranks everything
    assert hq_priority(spec, {'hq_priority': False}) is None
    request = dict(spec=spec, attempt_id='a', runtime_digest='d')
    with_priority = gpu_jobfile(request, tmp_path / 'r' / 'a', 'rsi.r', '/usr/bin/python3', '', priority=40)
    assert 'priority = 40' in with_priority
    assert 'priority = ' not in gpu_jobfile(request, tmp_path / 'r' / 'a', 'rsi.r', '/usr/bin/python3', '')


def test_hq_version_accepts_the_release_and_source_builds():
    assert hq_version("hyperqueue v0.26.2\n") == (0, 26, 2) and hq_version("hyperqueue 0.26.2-dev") == (0, 26, 2)
    assert hq_version("hyperqueue 0.27.0-dev") == (0, 27, 0) and hq_version("hyperqueue nightly") is None


def test_a_request_no_worker_can_hold_is_marked_infeasible_not_queued(tmp_path):
    from ecarsi.warm_pool.backend import infeasible
    from ecarsi.files import read, save
    from ecarsi.warm_pool.state import observation
    small, big = (6, 24576.0, 0, math.inf), (64, 118000.0, 1, math.inf)
    assert infeasible(dict(cpus=4, memory_mb=8192), [small]) is None
    assert infeasible(dict(cpus=12, memory_mb=8192), [small]).startswith("no worker holds 12 cpus / 8192 MB (largest 6 cpus")
    assert infeasible(dict(cpus=1, memory_mb=1024, gpu=dict(mode="required")), [small]).endswith("+ a GPU (largest 6 cpus / 24576 MB)")
    assert infeasible(dict(cpus=1, memory_mb=1024, gpu=dict(mode="required")), [small, big]) is None
    assert infeasible(dict(cpus=1, memory_mb=1024), []) == "no live worker"
    ending = (16, 29491.0, 0, 6414.0)
    assert infeasible(dict(cpus=4, memory_mb=12288, time_request_seconds=7230), [ending]) == \
        "no worker that holds 4 cpus / 12288 MB has 7230 s left"
    assert infeasible(dict(cpus=4, memory_mb=12288, time_request_seconds=7230), [ending, small]) is None
    # through _release: the observation names the reason, nothing is submitted, and a fitting worker frees it
    hq = bare()
    folder, attempt = tmp_path / "r", tmp_path / "r" / "a"
    attempt.mkdir(parents=True)
    spec = dict(request_id="r", operation_id="zoom-in.apply", cpus=12, memory_mb=8192, time_request_seconds=60)
    request = dict(spec=spec, attempt_id="a", submitted_at=1.0)
    save(folder / "request.json", request)
    cand = dict(c("r"), folder=folder, attempt=attempt, request=request, name="rsi.r.a", args=("submit",))
    worker = lambda cpus: {"configuration": {"resources": {"resources": [{"kind": "list", "name": "cpus", "values": list(range(cpus))},
                                                                          {"kind": "sum", "name": "mem", "size": 24576 * 10000}]}}}
    assert hq._release([cand], [], lambda: [worker(6)], "g") == []
    assert hq.release_state["infeasible"] == {"no worker holds 12 cpus / 8192 MB (largest 6 cpus / 24576 MB)": 1}
    assert observation(folder, read(folder / "request.json"))["infeasible"].startswith("no worker holds 12 cpus")
    assert len(hq._release([cand], [], lambda: [worker(16)], "g")) == 1 and hq.release_state["infeasible"] == {}


def test_an_idle_tick_publishes_the_idle_state():
    hq = bare()
    hq.release_state = dict(candidates=15, released=15, hq_waiting=27, infeasible={"x": 1}, gpu_slots=1)
    assert hq._release([], [{"task_stats": {"running": 0, "waiting": 0}}], lambda: [], "g") == []
    assert hq.release_state["hq_waiting"] == 0 and hq.release_state["candidates"] == 0 and hq.release_state["infeasible"] == {}
    assert hq.release_state["gpu_slots"] == 1   # what the idle tick did not look at keeps its last value
