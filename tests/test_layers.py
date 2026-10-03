"""Which subsystem may import which (docs/OVERVIEW.md, "The six parts"). A new import across a boundary
fails here; change these tables only together with the design.

The subsystems are the packages under ecarsi/. The modules directly in ecarsi/ are the vocabulary they
share (layout, files, contracts, run_state, review, ...): they import no subsystem at all."""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "ecarsi"

# subsystem: the subsystems it must not import
FORBIDDEN = {
    "ecarsi.warm_pool": {"ecarsi.control", "ecarsi.agent", "ecarsi.stages", "ecarsi.ui"},  # where and when, never what
    "ecarsi.agent": {"ecarsi.control", "ecarsi.stages", "ecarsi.ui"},                     # model turns only
    "ecarsi.stages": {"ecarsi.control", "ecarsi.agent", "ecarsi.ui"},                     # programs the pool runs
    "ecarsi.ui": {"ecarsi.control", "ecarsi.agent", "ecarsi.stages"},                     # read-only presentation
}
PARTS = {"ecarsi.control", "ecarsi.agent", "ecarsi.warm_pool", "ecarsi.stages", "ecarsi.ui"}
# Known crossings, each with its reason. Remove an entry when its import goes away.
ALLOWED = {
    ("ecarsi/stages/release.py", "ecarsi.ui.umapdata"): "release writes umap.json with Periscope's own writer",
    ("ecarsi/display.py", "ecarsi.ui"): "the display zone is what Periscope's renderer reads: display renders the pages to find out",
    ("ecarsi/observatory.py", "ecarsi.control"): "operator reports, run by a person, may ask Temporal directly (not a web page)",
    ("ecarsi/observatory.py", "ecarsi.warm_pool"): "operator reports read the pool's journals",
    ("ecarsi/observatory.py", "ecarsi.ui"): "operator reports and Periscope's /_control/ page show the same records",
}
KERNELS = {"msp", "osp", "zmip"}
# Only the programs of the compute image use the kernels; everything else also runs in the control image,
# which has none. They reach a kernel only through its api module, the kernel's contract with eca-rsi.
KERNEL_USERS = {"ecarsi.stages"}


def subsystem(module):
    parts = module.split(".")
    return ".".join(parts[:2]) if parts[0] == "ecarsi" and len(parts) > 1 else parts[0]


def imports():
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE.parent)
        package = ".".join(relative.parent.parts)
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = package.split(".")[: len(package.split(".")) - node.level + 1] if node.level else []
                module = ".".join(base + ([node.module] if node.module else []))
                # `from ..x import y` may name a module y: count both spellings
                names = [module] + [f"{module}.{alias.name}" for alias in node.names]
            else:
                continue
            for name in names:
                yield relative.as_posix(), ".".join(relative.with_suffix("").parts), name


def allowed(path, name):
    return any(path == where and (name == what or name.startswith(what + ".")) for where, what in ALLOWED)


def test_subsystems_import_only_downwards():
    crossings = set()
    for path, module, name in imports():
        source = subsystem(module)
        forbidden = FORBIDDEN.get(source, PARTS if source.startswith("ecarsi.") and source not in PARTS else ())
        if subsystem(name) in forbidden and not allowed(path, name):
            crossings.add(f"{path} imports {name}")
    assert not crossings, sorted(crossings)


def test_kernels_only_through_their_api():
    wrong = set()
    for path, module, name in imports():
        top = name.split(".")[0]
        if top not in KERNELS:
            continue
        if subsystem(module) not in KERNEL_USERS:
            wrong.add(f"{path} imports {name}: only {sorted(KERNEL_USERS)} may use the kernels")
        elif name not in KERNELS and not name.startswith(f"{top}.api"):
            wrong.add(f"{path} imports {name}: use {top}.api")
    assert not wrong, sorted(wrong)


def test_the_allowed_crossings_still_exist():
    gone = [entry for entry in ALLOWED if not any(allowed(path, name) and path == entry[0] for path, _, name in imports())]
    assert not gone, gone
