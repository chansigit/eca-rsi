"""Which subsystem may import which (docs/OVERVIEW.md, "The six parts"). A new import across a boundary
fails here; change these tables only together with the design.

The subsystems are the packages under ecarsi/. The modules directly in ecarsi/ are the vocabulary they
share (layout, files, contracts, run_state, review, ...): they import no subsystem at all. The kernels and
harness_bridge live in this repository too (decision 0018); MAY_IMPORT says which of its packages each may use."""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "ecarsi"

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
# which carries their source but not the numerical stack they need. They reach a kernel only through its api
# module, the kernel's contract with eca-rsi.
KERNEL_USERS = {"ecarsi.stages"}
# The repository's other packages (decision 0018): which of its own modules each may import (a name and what is
# under it). None imports ecarsi.
MAY_IMPORT = {  # the kernels run no agents since #28: eca-rsi's sessions do, so none imports harness_bridge
    "osp": set(),
    "msp": {"standissect_lite"},
    "zmip": {"msp.api"},  # like eca-rsi, through msp's contract module
    "standissect_lite": set(),
    "harness_bridge": set(),
}


def subsystem(module):
    parts = module.split(".")
    return ".".join(parts[:2]) if parts[0] == "ecarsi" and len(parts) > 1 else parts[0]


def imports(root=PACKAGE):
    for path in sorted(root.rglob("*.py")):
        yield from imports_of(path, root.parent)


def imports_of(path, top=REPO):
    relative = path.relative_to(top)
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


def test_the_repository_packages_import_only_what_they_may():
    own = set(MAY_IMPORT) | {"ecarsi"}
    wrong = {f"{path} imports {name}" for package, may in MAY_IMPORT.items() for path, _, name in imports(REPO / package)
             if name.split(".")[0] in own - {package} and not any(name == m or name.startswith(m + ".") for m in may)}
    assert not wrong, sorted(wrong)


SCRIPTS = sorted(path for folder in ("ops", "container") for path in (REPO / folder).glob("*.[ps][yh]"))


def resolves(name):
    """Whether a dotted name is a module or a name in one, e.g. ecarsi.control.temporal.endpoint."""
    import importlib
    parts = name.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            target = importlib.import_module(".".join(parts[:cut]))
        except ModuleNotFoundError:
            continue
        for attribute in parts[cut:]:
            if not hasattr(target, attribute):
                return False
            target = getattr(target, attribute)
        return True
    return False


def test_every_name_the_scripts_use_still_exists():
    """The scripts in ops/ and container/ import the repository's modules too, in Python or in the Python their
    shell lines run (#42: the pruner broke when 0.4.3 moved the files and only a test that happened to import it saw it)."""
    import re
    own = "|".join(["ecarsi"] + sorted(MAY_IMPORT))
    missing = set()
    for script in SCRIPTS:
        text = script.read_text()
        if script.suffix == ".py":
            names = [name for _, _, name in imports_of(script)]
        else:
            names = re.findall(rf"\b(?:{own})(?:\.\w+)+", text)
            names += [f"{module}.{n.strip()}" for module, listed in re.findall(rf"from ((?:{own})[\w.]*) import ([\w, ]+)", text)
                      for n in listed.split(",")]
        missing |= {f"{script.relative_to(REPO)}: {name}" for name in names
                    if name.split(".")[0] in {"ecarsi", *MAY_IMPORT} and not resolves(name)}
    assert not missing, sorted(missing)


def test_the_allowed_crossings_still_exist():
    gone = [entry for entry in ALLOWED if not any(allowed(path, name) and path == entry[0] for path, _, name in imports())]
    assert not gone, gone


# Stage programs: what control pins and the pool runs (stages.program). They share code only through helper
# modules (common, contract, ...), never through each other: zoom-in importing cross-sample made a change to
# persample.py invalidate queued zoom-in requests (2026-10-05).
PROGRAMS = {f"ecarsi.stages.{name}" for name in ("organize", "persample", "crosssample", "zoomin", "release", "display")}


def test_stage_programs_never_import_each_other():
    wrong = {f"{path} imports {name}" for path, module, name in imports()
             if module in PROGRAMS and name in PROGRAMS and name != module}
    assert not wrong, sorted(wrong)


def test_the_pool_names_no_stage_operation():
    """The pool runs what it is asked: which budget an operation or tool needs lives in stages.resources."""
    from ecarsi.stages.resources import CELL_MB, MEASURED_CEILING_MB, MEASURED_CPUS
    names = set(MEASURED_CEILING_MB) | set(MEASURED_CPUS) | set(CELL_MB)
    found = set()
    for path in sorted((PACKAGE / "warm_pool").glob("*.py")):
        tree = ast.parse(path.read_text())
        docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                      if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                      and node.body and isinstance(node.body[0], ast.Expr)}
        found |= {f"{path.name}: {node.value}" for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and node.value in names and id(node) not in docstrings}
    assert not found, sorted(found)

