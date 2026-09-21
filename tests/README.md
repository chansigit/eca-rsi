# Running the tests

Two interpreters see different halves of the suite; between them the whole suite runs green.

    S=/scratch/users/chensj16/projects      # the sibling kernel checkouts

    # science image: stages, warm pool, control plane, agent bridge, pages
    apptainer exec --cleanenv --bind /scratch,/oak,/home \
      --env PYTHONPATH=$PWD:/opt/rsi-control:/opt/rsi-python:/tmp/pytest-only \
      --env PYTHONNOUSERSITE=1 --env "ECA_SIBLINGS=$S/msp:$S/osp:$S/zmip" \
      /scratch/users/chensj16/containers/rsi-science-20260921-1.sif \
      /usr/local/bin/python3 -m pytest -q --continue-on-collection-errors tests

    # installed venv: the identity tests need package metadata for ecarsi
    /scratch/users/chensj16/venvs/eca-ct/python -m pytest -q tests/test_identity_scope.py tests/test_identity_provenance.py

`/tmp/pytest-only` is a node-local copy of pytest and its dependencies (the image has none):
copy `pytest _pytest pluggy iniconfig packaging py.py` out of
`/scratch/users/chensj16/venvs/eca-ct/.venv/lib/python3.12/site-packages`.

Neither the image nor the venv needs `distributed`: generation 2 schedules through HyperQueue and pins
`MSP_COMPUTE_ENDPOINT=local` in every task it runs, so nothing the workers execute imports dask. The gen-1
Dask pool and its tests were removed in 0.3.2; msp's own dask endpoints remain an optional extra it does not install.

Expected failures in the image run:

| test | why |
|---|---|
| `test_temporal_service.py::test_coordinator_reconnects_without_resubmitting_work` | timing test against a local Temporal server; times out under load, passes when the node is idle |

**Only `ecarsi` comes from the checkout.** `PYTHONPATH` puts `$PWD` first, but `msp` / `osp` / `zmip`
resolve to `/opt/rsi-python` inside the image — and so does the warm pool at run time, whose runtime
pythonpath is `[<eca-rsi worktree>, /opt/rsi-control, /opt/rsi-python]`. A kernel fix is therefore
**not deployed by committing it**: the image has to be rebuilt, or the checkout put ahead of
`/opt/rsi-python`. Both change the runtime digest, so both are batch-boundary work.
`rsi-science-20260921-1.sif` is the first image whose msp/zmip are byte-identical to their checkouts;
its predecessor carried the same version numbers with eleven files different, which is what kept
`test_zoomin_v2` red. Equal versions are not equal code — compare `/opt/rsi-runtime.json` digests.

`ECA_SIBLINGS` is a colon-separated list of checkout directories, each named after its package.
Without it `test_harness_sync.py` cannot find the kernels next to a worktree. It does **not** put
those checkouts on `sys.path` — see above.

The control-plane page has a browser-less smoke test: extract its inline script from disk and run
`node tests/observatory_page_smoke.js` (see the header of that file).
