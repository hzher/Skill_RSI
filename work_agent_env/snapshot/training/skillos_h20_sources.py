"""Compile pinned GPU algorithms with explicit deployment-only transformations.

No torch/CUDA import occurs here. Original bytes are verified before compiling;
every relocated literal and replaced deployment boundary is recorded for review.
"""
import ast
import builtins
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import types

from integrations.skill_generator.skillos.upstream import Upstream, VENDOR, SOURCE

EXTRA = VENDOR.parent / 'skillos_h20_runtime'
EXTRA_SHA256 = '17a1d85845af02d0293cf5a86074cd22c14945764645915816e997b968fdd4a0'
SP = 'runtime/pretrain-manager-v7/formal-launch-v52/efficiency-v62/storage-policy/'


class Sources:
    def __init__(self, config):
        self.upstream = Upstream()
        body = (EXTRA / 'UPSTREAM_MANIFEST.json').read_bytes()
        if hashlib.sha256(body).hexdigest() != EXTRA_SHA256:
            raise ValueError('Additional original runtime source manifest changed')
        self.extra = json.loads(body)
        for name, sha in self.extra['files'].items():
            if hashlib.sha256((EXTRA / name).read_bytes()).hexdigest() != sha:
                raise ValueError('Additional original runtime source changed: ' + name)
        self.config, self.audit = config, []
        self.relocations = {
            '/data/yangyang_nju/SkillOS-Storage-v86/models/Qwen3-8B': config['model_path'],
            '/data/yangyang_nju/SkillOS-Replication': config['storage_root'],
            '/home/yangyang_nju/SkillOS-Replication': config['control_root']}

    def load(self, name, *, extra=False, names=None, dependencies=None, namespace=None, transform=None):
        source = (EXTRA if extra else VENDOR) / name
        tree = ast.parse(source.read_text(), filename=str(source))
        if names is not None:
            tree.body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                         and node.name in names]
            if {node.name for node in tree.body} != set(names):
                raise ValueError('Original runtime definitions missing: ' + name)
        changes = []
        relocations = self.relocations
        class Relocate(ast.NodeTransformer):
            def visit_Constant(self, node):
                if isinstance(node.value, str):
                    for old in sorted(relocations, key=len, reverse=True):
                        if node.value == old or node.value.startswith(old + '/'):
                            value = relocations[old] + node.value[len(old):]
                            changes.append({'line': node.lineno, 'from': node.value, 'to': value})
                            return ast.copy_location(ast.Constant(value), node)
                return node
        tree = Relocate().visit(tree)
        if transform:
            tree, boundaries = transform(tree)
            changes.extend(boundaries)
        module = types.ModuleType('skillos_h20_' + name.replace('/', '_'))
        module.__file__ = str(source)
        module.__dict__.update(namespace or {})
        dependencies = dependencies or {}
        def imported(name, globals=None, locals=None, fromlist=(), level=0):
            if name in dependencies:
                return dependencies[name]
            return builtins.__import__(name, globals, locals, fromlist, level)
        module.__dict__['__builtins__'] = {**vars(builtins), '__import__': imported}
        exec(compile(ast.fix_missing_locations(tree), str(source), 'exec'), module.__dict__)
        self.audit.append({'source': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'selected_definitions': sorted(names) if names else 'whole_module', 'deployment_changes': changes})
        return module

    def storage(self):
        archive = self.load(SP + 'archive_authority.py', extra=True)
        engineering = self.load(SP + 'engineering_scope.py', extra=True)
        budget = self.load(SP + 'registered_budget.py', extra=True,
            dependencies={'archive_authority': archive, 'engineering_scope': engineering})
        guard = self.load(SP + 'guard.py', extra=True, dependencies={'registered_budget': budget})
        return guard, budget

    @staticmethod
    def actor_boundary(tree):
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'run')
        index = next(i for i, node in enumerate(function.body) if isinstance(node, ast.Assign)
                     and ast.unparse(node.targets[0]) == 'rank')
        function.body[:index] = ast.parse('''
permit = runtime.actor_admission(a)
batch, rows, totals, summary, app = runtime.load_contract(a)
if a.check_contract:
    print(json.dumps({'contract_valid': True, 'summary': summary, 'gpu_executed': False}))
    return
''').body
        return tree, [{'boundary': 'actor preamble', 'replacement': 'local run/update/model/lineage admission before original rank/FSDP/optimizer body'}]

    @staticmethod
    def validate_boundary(tree):
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        index = next(i for i, node in enumerate(main.body) if isinstance(node, ast.Assign)
                     and ast.unparse(node.targets[0]) == 'directory')
        main.body[:index] = ast.parse("a = runtime.validation_arguments()\n").body
        return tree, [{'boundary': 'fresh validation argv and deployment paths',
                      'replacement': 'bound local context; original fresh-process load/boundary/publish body retained'}]

    @staticmethod
    def export_boundary(tree):
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        index = next(i for i, node in enumerate(main.body) if isinstance(node, ast.Import)
                     and any(alias.name == 'torch' for alias in node.names))
        main.body[:index] = ast.parse("a = runtime.export_arguments()\n").body
        return tree, [{'boundary': 'export argv and deployment paths',
                      'replacement': 'bound local context; original CPU gloo full-shard merge retained'}]
