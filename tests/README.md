# Running the tests

The whole suite runs inside the compute image; nothing on the host is needed but pytest, which the images do not
carry (install it once with `pip install --target $GROUP_HOME/<user>/pytest-only pytest`; `ops/run.sh` adds it).

    bash ops/runsci-dev.sh -m pytest -q tests          # the dev worktree, every package

Since decision 0018 the kernels and `harness_bridge` are packages of this repository, so the checkout comes first on
`PYTHONPATH` for all of them: a kernel change is tested like any other change and reaches production with the next
image (or, once versions run side by side, the next published version). Their own tests are in `tests/osp/`,
`tests/msp/`, `tests/zmip/`, `tests/standissect_lite/` and `tests/harness_bridge/`.

`tests/conftest.py` sets `ECARSI_STRICT=1` (a degraded step re-raises, decision 0013) and restores `os.environ`
around each test. One bridge test needs `node` and skips without it. The control image lacks the numerical stack:
only the control and warm-pool tests collect there.

The control-plane page has a browser-less smoke test: extract its inline script from disk and run
`node tests/observatory_page_smoke.js` (see the header of that file).
