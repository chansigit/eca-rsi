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
    tick(0, ["wide"]); assert hq.release_state["draining"]
    tick(50, ["wide"]); assert hq.drain_since == 0
    tick(101, ["wide"]); assert not hq.release_state["draining"] and hq.cooldown_until == 201
    tick(150, ["wide"]); assert not hq.release_state["draining"]   # yielding
    tick(202, ["wide"]); assert hq.release_state["draining"]       # drains again
    tick(203, []); assert hq.drain_since is None
