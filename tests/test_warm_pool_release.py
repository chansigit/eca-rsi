from ecarsi.warm_pool.backend import release_plan, worker_capacity


def c(key, klass="work", t=0, gpu=False):
    return dict(key=key, klass=klass, submitted_at=t, gpu_preferred=gpu)


KW = dict(gpu_waiting=0, gpu_slots=0, gpu_seconds=120, cpu_seconds=360)


def test_short_hq_queue_fifo_and_interactive_first():
    cands = [c("deg-new", t=5), c("deg-old", t=1), c("tool", "tool", t=9), c("agent", "agent", t=9)]
    plan = release_plan(cands, hq_waiting=14, cap=16, draining=False, **KW)
    # model call and tool bypass the backlog cap; work fills the two free places oldest first
    assert [k for k, _ in plan] == ["agent", "tool", "deg-old", "deg-new"]
    plan = release_plan(cands, hq_waiting=15, cap=16, draining=False, **KW)
    assert [k for k, _ in plan] == ["agent", "tool", "deg-old"]


def test_drain_holds_batch_work_only():
    cands = [c("deg"), c("tool", "tool"), c("agent", "agent")]
    assert release_plan(cands, hq_waiting=0, cap=16, draining=True, **KW) == [("agent", False), ("tool", False)]


def test_gpu_preferred_pinned_to_gpu_only_while_its_queue_is_short():
    cands = [c(f"compute-{i}", t=i, gpu=True) for i in range(6)]
    plan = release_plan(cands, hq_waiting=0, cap=16, draining=False,
                        gpu_waiting=1, gpu_slots=1, gpu_seconds=120, cpu_seconds=360)
    # 1 and 2 ahead on one GPU clear within a CPU run (240 s < 360 s): GPU-only; from 3 ahead, either
    assert [g for _, g in plan] == [True, True, False, False, False, False]
    plan = release_plan(cands, hq_waiting=0, cap=16, draining=False,
                        gpu_waiting=1, gpu_slots=4, gpu_seconds=120, cpu_seconds=360)
    assert all(g for _, g in plan)  # four GPUs: every one clears sooner than a CPU run


def test_worker_capacity_reads_hq_json():
    w = {"ended": None, "configuration": {"resources": {"resources": [
        {"kind": "list", "name": "cpus", "values": ["0", "1", "2", "3"]},
        {"kind": "sum", "name": "mem", "size": 294910000},
        {"kind": "list", "name": "gpuSlot/0", "values": ["uuid#0"]}]}}}
    assert worker_capacity([w, dict(w, ended="x")]) == [(4, 29491.0, 1)]


def test_drain_starts_on_an_aged_task_and_yields_after_its_limit():
    from ecarsi.warm_pool.backend import HyperQueue, RELEASE_DEFAULTS
    hq = HyperQueue.__new__(HyperQueue)
    hq.release = dict(RELEASE_DEFAULTS, max_drain_seconds=100)
    hq.drain_since = hq.cooldown_until = None
    tick = lambda now, aged: hq._release([], [], aged, 0, lambda: [], "g", now) or hq.release_state
    fresh, old = [("wide", 0)], [("wide", 3 * 3600)]
    tick(0, fresh); assert hq.release_state["draining"]
    tick(50, fresh); assert hq.drain_since == 0
    tick(101, fresh); assert not hq.release_state["draining"] and hq.cooldown_until == 201
    tick(150, fresh); assert not hq.release_state["draining"]   # yielding
    tick(202, fresh); assert hq.release_state["draining"]       # drains again
    tick(203, []); assert hq.drain_since is None
    # a task waiting 3 h: drains 4x as long, yields a quarter as long
    tick(1000, old); tick(1350, old); assert hq.release_state["draining"]
    tick(1401, old); assert not hq.release_state["draining"] and hq.cooldown_until == 1401 + 25


def test_backlog_cap_fills_idle_cpus_first(tmp_path):
    from ecarsi.warm_pool.backend import HyperQueue, RELEASE_DEFAULTS
    hq = HyperQueue.__new__(HyperQueue)
    hq.release, hq.drain_since, hq.cooldown_until = dict(RELEASE_DEFAULTS), None, None
    worker = {"configuration": {"resources": {"resources": [{"kind": "list", "name": "cpus", "values": list(range(48))}]}}}
    jobs = [{"task_stats": {"running": 1, "waiting": 0}}] * 10
    gone = dict(c("x"), folder=tmp_path, attempt=tmp_path, request={})   # request.json vanished: skipped
    hq._release([gone], jobs, [], 0, lambda: [worker], "g", 0)
    assert hq.release_state["backlog_cap"] == 16 + 38   # short queue + the 38 idle CPUs


def test_release_knobs_follow_config_edits(tmp_path):
    import json, os
    from ecarsi.warm_pool.backend import HyperQueue, RELEASE_DEFAULTS
    hq = HyperQueue.__new__(HyperQueue)
    hq.root, hq.release, hq._release_stamp = tmp_path, dict(RELEASE_DEFAULTS), None
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"release": {"backlog_per_cpu": 1}}))
    hq._reload_release(); assert hq.release["backlog_per_cpu"] == 1
    config.write_text(json.dumps({"release": {"backlog_per_cpu": 2, "typo": 1}})); os.utime(config, ns=(1, 1))
    hq._reload_release(); assert hq.release["backlog_per_cpu"] == 1   # unknown knob: keep what is in force
    config.write_text(json.dumps({"release": {"drain_age_seconds": 60}})); os.utime(config, ns=(2, 2))
    hq._reload_release(); assert hq.release["drain_age_seconds"] == 60 and hq.release["backlog_per_cpu"] == 1 / 3


def test_scheduler_uses_a_separate_hq_server_only_while_one_holds_the_lock(tmp_path):
    from ecarsi.warm_pool.backend import external_hq_server
    from ecarsi.warm_pool.state import lock
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
