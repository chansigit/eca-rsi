"""Loaded by every pool executor process (worker.py puts this directory first on PYTHONPATH).

fast-array-utils 1.5 builds its numba sparse types with `type(f"{cls.__name__}Type", ...)` and never
binds them to a module name, so pickle stores each class by value and every process that reads the
numba cache back gets a brand-new class: no cached signature ever equals a live one. Every call
missed and appended to the index; 7,019 entries made one aggregate call take 195 s (2026-09-23).
Naming the classes in their module makes pickle store them by reference, and the cache hits.

Lazy: nothing is imported until something else imports that module, so tasks that never touch
numba pay nothing. Remove once fast-array-utils names its types itself."""
import importlib.abc
import sys

TARGET = "fast_array_utils._plugins.numba_sparse"


def _name_types(module):
    for cls in list(getattr(module, "TYPES", ())) + list(getattr(module, "MODELS", {}).values()):
        cls.__module__, cls.__qualname__ = module.__name__, cls.__name__
        setattr(module, cls.__name__, cls)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != TARGET:
            return None
        sys.meta_path.remove(self)
        try:
            from importlib.util import find_spec
            spec = find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return spec
        run = spec.loader.exec_module
        def exec_module(module):
            run(module)
            _name_types(module)
        spec.loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, _Finder())
