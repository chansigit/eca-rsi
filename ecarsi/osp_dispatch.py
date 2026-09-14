"""OSP process dispatch, deployable separately from the pinned scientific kernels."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from harness_bridge.control import pausable

from ecarsi import layout as L
from ecarsi.run_state import read_json, write_json, writer_lock, file_identity

def released(unit):
    """A verified release may have pruned OSP matrices; never regenerate it."""
    if not (L.release_dir(unit)/'.rsi-release.json').is_file():
        return False
    from ecarsi.downstream import check_release, unit_lock
    with unit_lock(unit):
        check_release(unit)
    print(f'[persample] verified released unit; skipping: {unit.name}', flush=True)
    return True

def _driver_estimate_bytes(entry: dict) -> int:
    """Remote compute reserves worker memory separately; budget annotation here."""
    from ecarsi.persample import _estimate_bytes, FIXED_BYTES_PER_CHILD, SUBSET_FILE
    full = _estimate_bytes(entry["n_cells"])
    if os.environ.get("OSP_COMPUTE_ENDPOINT", "local") != "pool":
        return full  # auto may still choose local computation
    subset = Path(entry["outdir"]) / SUBSET_FILE
    if not subset.is_file():
        return full
    # Subsets are written uncompressed. Allow copies plus Python/kernel imports;
    # the worker still uses the full computation estimate in run_compute().
    estimate = min(full, FIXED_BYTES_PER_CHILD + 4 * subset.stat().st_size)
    # Annotation loads the derived matrix too, which can be larger than the
    # raw subset. Once compute has finished, use that concrete size as well.
    derived = [p.stat().st_size for p in (subset.parent/'computed.h5ad', subset.parent/'clustered.h5ad') if p.is_file()]
    return max(estimate, FIXED_BYTES_PER_CHILD + 4 * max(derived)) if derived else estimate


def drive(pending: list[dict], out_root: Path, annotate: bool, on_done=None) -> list[dict]:
    """Run every pending sample's command as a child process under the
    concurrency plan; one retry per sample; failures go to failures.md.
    Returns the entries that did not finish. In pool mode, computed samples
    enter a bounded annotation queue so model calls do not hold compute slots.

    AGENT_MODEL_POOL_ROTATE=1 (with AGENT_MODEL_POOL set) hands the Nth
    launched worker a copy of the pool rotated by N instead of the same
    unrotated spec every worker would otherwise resolve fresh in its own
    process -- spreads first attempts across an equally-trusted pool so
    many concurrent samples don't all hit the same candidate at once
    (eca-rsi#7). A retried sample gets the next launch's rotation, not the
    one that just failed. Off by default: without it every worker prefers
    the same primary, which is the right default for a quality-ranked
    fallback list rather than equally-trusted alternatives."""
    import resource
    from ecarsi.persample import plan_concurrency, _pump, is_done, is_empty, SUBSET_FILE

    from harness_bridge.control import PauseRequested, pause_requested

    if not pending:
        return []
    if os.environ.get("OSP_COMPUTE_ENDPOINT", "local") == "local":
        pending = sorted(pending, key=lambda e: -e["n_cells"])  # local memory planning
    max_parallel, budget, threads = plan_concurrency(pending)
    staged = annotate and os.environ.get("OSP_COMPUTE_ENDPOINT", "local") == "pool"
    if staged:
        threads = max(1, threads // 2)
    from ecarsi.resources import available_cpus, available_memory_bytes

    print(f"[drive] {len(pending)} sample(s), up to {max_parallel} at once, {threads} thread(s) each, "
          f"memory budget {budget / 2**30:.1f} GiB ({available_cpus()} cpu(s), "
          f"{available_memory_bytes() / 2**30:.1f} GiB available)", flush=True)
    if staged:
        print(f"[drive] separate compute and annotation slots: {max_parallel} each, shared memory budget", flush=True)
    env = dict(os.environ)
    # Also supports OSP_PYTHON with only the kernel installed.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMBA_NUM_THREADS",
              "MSP_MAX_THREADS"):
        env[k] = str(threads)
    pool_spec = env.get("AGENT_MODEL_POOL", "")
    rotate = bool(pool_spec) and env.get("AGENT_MODEL_POOL_ROTATE", "").strip() not in ("", "0")
    launched = 0

    queue = [(e, "compute" if staged else "full") for e in pending]
    running: dict[str, tuple] = {}  # value -> (proc, est, t0, entry, tail, phase)
    attempts: dict[tuple[str, str], int] = {}
    sample_started: dict[str, float] = {}
    failed: list[dict] = []
    paused = False
    while queue or running:
        for value in list(running):
            proc, est, t0, e, tail, phase = running[value]
            rc = proc.poll()
            if rc is None:
                continue
            del running[value]
            took = (time.time() - t0) / 60
            outdir = Path(e["outdir"])
            state = read_json(outdir / L.RUN_STATE) if (outdir / L.RUN_STATE).is_file() else {}
            if (rc == 0 and phase == "compute" and state.get("state") == "computed"
                    and state.get("identity") == e.get("identity")):
                first_compute = next((i for i, (_, p) in enumerate(queue) if p == "compute"), len(queue))
                queue.insert(first_compute, (e, "annotation"))
                print(f"[drive] {value} compute ready after {took:.1f} min; queued for annotation", flush=True)
            elif rc == 0 and is_done(outdir, annotate, e.get("identity")):
                if staged:
                    took = (time.time() - sample_started[value]) / 60
                print(f"[drive] {value} done in {took:.1f} min", flush=True)
                (outdir / SUBSET_FILE).unlink(missing_ok=True)
                (outdir / "computed.h5ad").unlink(missing_ok=True)
                (outdir / "compute_state.json").unlink(missing_ok=True)
                if on_done:
                    on_done(e, took)
            elif rc == 3:
                paused = True
                print(f"[drive] {value} paused after {took:.1f} min", flush=True)
            elif is_empty(outdir, e.get("identity")):
                # QC removed every cell: nothing to cluster, nothing lost —
                # all of them are in qc_removed.csv with a reason
                print(f"[drive] {value}: no cell passed OSP QC after {took:.1f} min — kept as an empty sample "
                      f"({e['n_cells']} cells, all in qc_removed.csv); not offered to integration", flush=True)
                (outdir / SUBSET_FILE).unlink(missing_ok=True)
                if on_done:
                    on_done(e, took)
            elif attempts[value, phase] < 2 and state.get("retryable") is True:
                print(f"[drive] {value} FAILED (exit {rc}) after {took:.1f} min — retrying once", flush=True)
                queue.append((e, phase))
            else:
                print(f"[drive] {value} FAILED (exit {rc}) after {took:.1f} min — recorded, moving on",
                      flush=True)
                with open(out_root / "failures.md", "a") as f:
                    f.write(f"## {value} ({e['n_cells']} cells) — exit {rc}, {time.strftime('%Y-%m-%d %H:%M')}\n\n"
                            f"command: `{shlex.join(e['command'])}`\n\n```\n" + "\n".join(tail) + "\n```\n\n")
                failed.append(e)
        used = sum(item[1] for item in running.values())
        paused = paused or pause_requested()
        while queue and not paused and not pause_requested():
            # Each phase has the existing concurrency cap, and both share the
            # same driver memory budget. Bound computed-but-unannotated work too.
            eligible = next((i for i, (entry, phase) in enumerate(queue)
                             if sum(item[5] == phase for item in running.values()) < max_parallel
                             and (phase != "compute" or
                                  sum(p == "annotation" for _, p in queue) < max_parallel)
                             and (not running or used + _driver_estimate_bytes(entry) <= budget)), None)
            if eligible is None:
                break
            e, phase = queue[eligible]
            est = _driver_estimate_bytes(e)
            queue.pop(eligible)
            value = e["value"]
            outdir = Path(e["outdir"])
            outdir.mkdir(parents=True, exist_ok=True)
            attempts[value, phase] = attempts.get((value, phase), 0) + 1
            tail: deque = deque(maxlen=40)
            child_env = env
            if rotate and phase != "compute":
                from harness_bridge import rotate_model_pool
                child_env = {**env, "AGENT_MODEL_POOL": rotate_model_pool(pool_spec, launched)}
            launched += phase != "compute"
            command = e["command"] + (["--compute-only"] if phase == "compute" else [])
            sample_started.setdefault(value, time.time())
            proc = subprocess.Popen(command, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, env=child_env, bufsize=1,
                                    cwd=str(outdir))
            threading.Thread(target=_pump, args=(proc, value, tail, out_root.parent if out_root.name == L.PERSAMPLE else None), daemon=True).start()
            running[value] = (proc, est, time.time(), e, tail, phase)
            used += est
            print(f"[drive] {value} {phase} started (attempt {attempts[value, phase]}): {e['n_cells']} cells, "
                  f"est {est / 2**30:.1f} GiB, {len(running)} running, {len(queue)} waiting", flush=True)
        if paused and not running:
            break
        if running:
            time.sleep(1 if staged else 5)
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * 1024
    print(f"[drive] peak child RSS {peak / 2**30:.1f} GiB (largest sample {pending[0]['n_cells']} cells; "
          f"tune PERSAMPLE_MEM_PER_CELL_MB from this)", flush=True)
    if paused:
        error = PauseRequested()
        error.failed_samples = failed
        raise error
    return failed


@pausable
def main(argv=None):
    """Prepare/finalize with the installed RSI; independently dispatch OSP phases.

    This entry point can live in an operational namespace while ecarsi and OSP
    remain pinned. It never rewrites their input, configuration or runtime IDs.
    """
    import pandas as pd
    from harness_bridge.control import PauseRequested
    from ecarsi import persample as ps
    from ecarsi.osp_contract import is_finished, REQUEST

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('unit', type=Path)
    parser.add_argument('--sample-map')
    args = parser.parse_args(argv)
    unit = args.unit.resolve()
    if released(unit):
        return 0
    original_args = [str(unit)] + (['--sample-map', args.sample_map] if args.sample_map else [])
    rc = ps.main([*original_args, '--plan-only'])
    if rc:
        return rc
    out = L.persample_root(unit)
    with writer_lock(out/'.driver.lock'):
        manifest = read_json(out/L.MANIFEST)
        config = manifest['config']
        entries = ps.build_entries(Path(manifest['h5ad']), ps.SAMPLE_KEY,
                                   {s['value']: s['n_cells'] for s in manifest['samples']},
                                   out, os.environ.get('OSP_PYTHON', sys.executable),
                                   config['annotate'], config['model'])
        identities = {s['value']: s['identity'] for s in manifest['samples']}
        for entry in entries:
            entry['identity'] = identities[entry['value']]
            entry['request'] = dict(identity=entry['identity'], config=config, runtime=manifest['runtime'],
                                    value=entry['value'], n_cells=entry['n_cells'], context=L.report_context(unit))
            entry['command'] = [entry['command'][0], '-m', __package__+'.osp_stage',
                                str(Path(entry['outdir'])/REQUEST)]
        pending = [e for e in entries if not is_finished(Path(e['outdir']), config['annotate'], e['identity'])]
        if pending:
            write_json(out/'orchestration.json', dict(schema_version=1, module=__name__,
                       sources={p.name: file_identity(p) for p in (Path(__file__), Path(__file__).with_name('osp_stage.py'))},
                       kernel_runtime=manifest['runtime'], started_at=time.time()))
            # This adapter also runs against an older pinned ecarsi package.
            # Set the index after parsing, so pandas cannot infer integer IDs.
            table = pd.read_csv(out/L.SAMPLE_MAPPING, dtype=str, keep_default_na=False)
            table = table.set_index(table.columns[0])
            if ps.mapping_identity(table) != manifest['mapping_identity']:
                raise ValueError('recorded cell/sample mapping changed')
            ps.write_subsets(Path(manifest['h5ad']), table, pending, manifest['sample_mapping'].get('batch_key'))
            manifest['state'] = 'running'
            write_json(out/L.MANIFEST, manifest)
            paused = False
            try:
                failed = drive(pending, out, config['annotate'], on_done=lambda e, took: ps._pages(unit))
            except PauseRequested as exc:
                paused, failed = True, exc.failed_samples
            if paused or failed:
                manifest.update(state='failed' if failed else 'paused',
                                failed_samples=[e['value'] for e in failed],
                                pending_samples=[e['value'] for e in entries
                                                 if not is_finished(Path(e['outdir']), config['annotate'], e['identity'])])
                write_json(out/L.MANIFEST, manifest)
                ps._pages(unit)
                return 1 if failed else 3
    # The installed finalizer rechecks every original identity and cell ledger.
    # Successful samples are skipped; no second annotation/compute is requested.
    return ps.main(original_args)


if __name__ == '__main__':
    from harness_bridge import configure_logging
    configure_logging('ecarsi', stream=sys.stderr)
    raise SystemExit(main())
