# Running the tests

The whole suite runs inside the compute image; nothing on the host is needed but pytest and ruff, which the images
do not carry (install them once into `$GROUP_HOME/<user>/pytest-only` with `pip install --target`, and put ruff's
binary from its wheel into `pytest-only/bin/`; `ops/run.sh` adds the directory, and `tests/test_lint.py` runs ruff).

    bash ops/runsci-dev.sh -m pytest -q tests          # the dev worktree, every package

Since decision 0018 the kernels and `harness_bridge` are packages of this repository, so the checkout comes first on
`PYTHONPATH` for all of them: a kernel change is tested like any other change and reaches production with the next
image (or, once versions run side by side, the next published version). Their own tests are in `tests/osp/`,
`tests/msp/`, `tests/zmip/`, `tests/standissect_lite/` and `tests/harness_bridge/`.

`tests/conftest.py` sets `ECARSI_STRICT=1` (a degraded step re-raises, decision 0013) and restores `os.environ`
around each test. One bridge test needs `node` and skips without it. The control image lacks the numerical stack:
only the control and warm-pool tests collect there. `SCIENCE_ONLY` in `tests/conftest.py` lists the modules that
need the compute image; with `ECA_TESTS=control` they are not collected. GitHub runs that subset and the lint rules
on every push (`.github/workflows/tests.yml`, #49); the Sherlock suite stays the release check.

The control-plane page has a browser-less smoke test: extract its inline script from disk and run
`node tests/observatory_page_smoke.js` (see the header of that file).
