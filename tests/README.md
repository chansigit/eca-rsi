# Running the tests

Two interpreters see different halves of the suite; between them everything except the
`distributed` (dask) tests runs green.

    S=/scratch/users/chensj16/projects      # the sibling kernel checkouts

    # science image: stages, warm pool, control plane, agent bridge, pages
    apptainer exec --cleanenv --bind /scratch,/oak,/home \
      --env PYTHONPATH=$PWD:/opt/rsi-control:/opt/rsi-python:/tmp/pytest-only \
      --env PYTHONNOUSERSITE=1 --env "ECA_SIBLINGS=$S/msp:$S/osp:$S/zmip" \
      /scratch/users/chensj16/containers/rsi-science-20260917-1.sif \
      /usr/local/bin/python3 -m pytest -q --continue-on-collection-errors tests

    # installed venv: the identity tests need package metadata for ecarsi
    /scratch/users/chensj16/venvs/eca-ct/python -m pytest -q tests/test_identity_scope.py tests/test_identity_provenance.py

`/tmp/pytest-only` is a node-local copy of pytest and its dependencies (the image has none):
copy `pytest _pytest pluggy iniconfig packaging` out of
`/scratch/users/chensj16/venvs/eca-ct/.venv/lib/python3.12/site-packages`.

Expected failures in that run:

| test | why |
|---|---|
| `test_pool_multitask.py` (collection), `test_pool_observe.py` (2), `test_compute_policy.py::test_gpu_probe_requires_a_free_compatible_slot` | `distributed` (dask) is not installed in the science image; these cover the optional dask compute endpoint |
| `test_temporal_service.py::test_coordinator_reconnects_without_resubmitting_work` | timing test against a local Temporal server; times out under load, passes when the node is idle |

`ECA_SIBLINGS` is a colon-separated list of checkout directories, each named after its package.
Without it `test_harness_sync.py` cannot find the kernels next to a worktree.

The control-plane page has a browser-less smoke test: extract its inline script from disk and run
`node tests/observatory_page_smoke.js` (see the header of that file).
