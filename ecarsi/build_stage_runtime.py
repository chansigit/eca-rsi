"""Build a separate control package; all numerical kernels stay in installed packages."""
import argparse
import ast
import importlib.util
from pathlib import Path
import shutil

MODULES = {
    'msp': ['__main__', 'inspect', 'annotate', 'agent_data', 'agent_tools', 'checkpoint', 'evidence'],
    'zmip': ['__main__', 'lineage', 'annotate', 'plan'],
}


def build(destination, msp, zmip, namespace):
    destination = Path(destination)/namespace
    destination.mkdir(parents=True, exist_ok=False)
    (destination/'__init__.py').touch()
    selected = {f'{package}.{name}' for package, names in MODULES.items() for name in names}

    def mapped(module):
        return f'{namespace}.{module}' if module in selected else module

    class Imports(ast.NodeTransformer):
        def __init__(self, package):
            self.package = package

        def visit_ImportFrom(self, node):
            original = importlib.util.resolve_name('.'*node.level+(node.module or ''), self.package) if node.level else node.module
            nodes = []
            for alias in node.names:
                full = original+'.'+alias.name
                parent = f'{namespace}.{original}' if full in selected else mapped(original)
                nodes.append(ast.ImportFrom(module=parent, names=[alias], level=0))
            return nodes

        def visit_Call(self, node):
            # CLI logging config names the original package families. Keep
            # that routing when orchestration lives in a versioned namespace.
            if (isinstance(node.func, ast.Attribute) and node.func.attr == 'getLogger'
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == 'logging'
                    and len(node.args) == 1 and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == '__name__'):
                node.args[0] = ast.Constant(self.package)
            return self.generic_visit(node)

    for package, base in [('msp', Path(msp)), ('zmip', Path(zmip))]:
        target = destination/package
        target.mkdir()
        (target/'__init__.py').touch()
        for name in MODULES[package]:
            source = base/package/(name+'.py')
            tree = Imports(package).visit(ast.parse(source.read_text()))
            (target/(name+'.py')).write_text(ast.unparse(ast.fix_missing_locations(tree))+'\n')
    for name in ['driver_budget.py', 'stage_python.py']:
        shutil.copy2(Path(__file__).with_name(name), destination/name)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--msp', type=Path, required=True)
    parser.add_argument('--zmip', type=Path, required=True)
    parser.add_argument('--namespace', required=True)
    args = parser.parse_args()
    print(build(args.destination, args.msp, args.zmip, args.namespace))


def build_preparation(destination, kernel, namespace, dispatch_module='ecarsi.osp_dispatch'):
    """Keep the original planning contracts, using only obs and sliced counts."""
    root = Path(destination)/namespace
    root.mkdir(parents=True, exist_ok=False)
    (root/'__init__.py').touch()

    class Planning(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            original = importlib.util.resolve_name('.'*node.level+(node.module or ''), 'ecarsi') if node.level else node.module
            if original in {'ecarsi.sample_mapping', 'ecarsi.persample', 'ecarsi.osp_contract'}:
                original = namespace+'.'+original.rsplit('.', 1)[1]
            elif original == 'ecarsi.osp_dispatch':
                original = dispatch_module
            node.module, node.level = original, 0
            return node

        def visit_Call(self, node):
            if (isinstance(node.func, ast.Attribute) and node.func.attr == 'read_h5ad'
                    and any(k.arg == 'backed' and isinstance(k.value, ast.Constant) and k.value.value == 'r'
                            for k in node.keywords)):
                return ast.Call(func=ast.Name(id='_metadata_data', ctx=ast.Load()), args=node.args, keywords=[])
            return self.generic_visit(node)

    for name in ('persample', 'sample_mapping', 'osp_contract'):
        tree = Planning().visit(ast.parse((Path(kernel)/(name+'.py')).read_text()))
        text = ast.unparse(ast.fix_missing_locations(tree))
        # Future imports must stay first; place this immediately before the
        # first ordinary import in the transformed module.
        tree = ast.parse(text)
        index = next(i for i, node in enumerate(tree.body) if isinstance(node, (ast.Import, ast.ImportFrom))
                     and not (isinstance(node, ast.ImportFrom) and node.module == '__future__'))
        if name != 'osp_contract':
            tree.body.insert(index, ast.ImportFrom(module=namespace+'.prepare_osp',
                             names=[ast.alias(name='metadata_data', asname='_metadata_data')], level=0))
        (root/(name+'.py')).write_text(ast.unparse(ast.fix_missing_locations(tree))+'\n')
    for name in ('prepare_osp.py', 'osp_stage.py', 'driver_budget.py'):
        shutil.copy2(Path(__file__).with_name(name), root/name)
    (root/'prompts').mkdir()
    shutil.copy2(Path(kernel)/'prompts/sample_column.md', root/'prompts/sample_column.md')
    return root


if __name__ == '__main__':
    main()
