"""Load pinned SkillOS functions without its retired server/job lifecycle.

Original files remain byte-identical. AST selection avoids executing imports of
old CONTROL/gates. Only the ALF/Web executor block is replaced in ``rollout``;
its Curator/judge/edit loop and ``finalize_group`` are compiled from upstream.
"""
import ast
import asyncio
import builtins
import copy
import hashlib
import json
import math
from pathlib import Path
import types

VENDOR = Path(__file__).resolve().parents[1] / 'vendor/skillos_upstream'
SOURCE = 'snapshots/2026-10-29/source204/'
REVISION = '3c039efeafd89400115426a9a7af19b58601f0be'
MANIFEST_SHA256 = 'fb0cd968ae2ed1df1722a341e4fbdee0c55f960bd43ccc7ebd36dc84500ba985'


def verify_sources():
    body = (VENDOR / 'UPSTREAM_MANIFEST.json').read_bytes()
    if hashlib.sha256(body).hexdigest() != MANIFEST_SHA256:
        raise ValueError('SkillOS immutable source manifest changed')
    manifest = json.loads(body)
    if manifest['source_commit'] != REVISION:
        raise ValueError('SkillOS source revision changed')
    for name, expected in manifest['files'].items():
        if hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() != expected:
            raise ValueError('SkillOS source changed: ' + name)
    return manifest


def _module(path, *, names=None, dependencies=None, namespace=None):
    """Compile original definitions, with module-local imports (no sys.path edits)."""
    module = types.ModuleType('pinned_skillos_' + path.replace('/', '_'))
    module.__file__ = str(VENDOR / path)
    module.__dict__.update(namespace or {})
    imports = dependencies or {}
    def import_module(name, globals=None, locals=None, fromlist=(), level=0):
        if name in imports:
            return imports[name]
        return builtins.__import__(name, globals, locals, fromlist, level)
    module.__dict__['__builtins__'] = {**vars(builtins), '__import__': import_module}
    tree = ast.parse((VENDOR / path).read_text(), filename=module.__file__)
    if names is not None:
        tree.body = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                     and node.name in names]
        if {n.name for n in tree.body} != set(names):
            raise ValueError('Upstream definition missing: ' + path)
    exec(compile(tree, module.__file__, 'exec'), module.__dict__)
    return module


class Upstream:
    def __init__(self):
        self.manifest = verify_sources()
        self.io = _module(SOURCE + 'rollout_backend/run_state.py', names={
            'ContractError', 'sha', 'canonical', 'file_sha', 'require', 'save', 'read', 'ref', 'verify_ref'},
            namespace={'Path': Path, 'hashlib': hashlib, 'json': json, 'os': __import__('os')})
        dependencies = {'run_state': self.io}
        self.reward = _module(SOURCE + 'rollout_backend/yaml_reward_toolchain.py')
        dependencies.update(reward_toolchain=self.reward, yaml_reward_toolchain=self.reward)
        self.renderer = _module('implementation/curator_render.py', dependencies=dependencies)
        dependencies['curator_render'] = self.renderer
        math_adapter = types.SimpleNamespace(render_math_curator=None)
        self.protocol = _module(SOURCE + 'rollout_backend/curator_protocol.py',
            dependencies={**dependencies, 'math_adapter': math_adapter})
        self.prompts = _module('sources/audit/skillos/curator/prompts.py')
        self.roles = _module(SOURCE + 'rollout_backend/role_client.py', names={'validate_response'},
            namespace={**vars(self.io), 'math': math})
        self.helpers = _module(SOURCE + 'rollout_backend/rollout_alfweb.py',
            names={'parse_selfjudge', 'selfjudge_attempt', 'curator_result_feedback', 'group_reward_masks', 'bm25'},
            dependencies=dependencies, namespace={**vars(self.io), 'math': math, 'ROOT': VENDOR,
                # group_reward_masks prepends the old import root. Imports are
                # already isolated/pinned here; do not mutate global sys.path.
                'sys': types.SimpleNamespace(path=types.SimpleNamespace(insert=lambda *_: None))})
        self.scheduler = _module(SOURCE + 'rollout_backend/group_window.py')
        self.requests = _module(SOURCE + 'rollout_backend/request_lifecycle.py', dependencies={'run_state': self.io})
        self.actor_core = _module(SOURCE + 'actor_backend/core.py', names={'canonical_json', 'logprobs'},
            namespace={'json': json})
        self.actor_contract = _module(SOURCE + 'actor_backend/contract.py',
            names={'validate_batch', 'validate_application', 'checked_reference', 'verify_reward_sources'},
            namespace={'Path': Path, 'hashlib': hashlib, 'json': json, 'math': math, 're': __import__('re'),
                       'canonical_json': self.actor_core.canonical_json, 'digest_file': self.io.file_sha})
        self.actor_math = _module(SOURCE + 'actor_backend/four_rank_math.py')

    def collector(self, *, journal, plan, role_manifest, tokenizer, clients, executor, correctness_prompt):
        path = VENDOR / SOURCE / 'rollout_backend/rollout_alfweb.py'
        tree = ast.parse(path.read_text())
        collect = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'collect')
        functions = {n.name: copy.deepcopy(n) for n in ast.walk(collect)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in {'rollout', 'finalize_group'}}
        rollout = functions['rollout']
        position_loop = next(n for n in ast.walk(rollout) if isinstance(n, ast.For)
                             and ast.unparse(n.iter) == "enumerate(group['ordered_task_ids'])")
        body = position_loop.body
        start = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                     and ast.unparse(n.targets[0]) == 'episode_id')
        end = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                   and ast.unparse(n.targets[0]) == 'trace')
        # The sole domain execution replacement. Everything after trace (judge,
        # original render, edit, content score, compression) is unchanged.
        body[start:end] = ast.parse('''
initial, envstate, trajectory, outcome, retrieved, record, raw_refs = await executor.run_position(
    group_info=group_info, rollout=n, position=pos, repo=repo, root=root, identity=journal.identity)
''').body
        # Describe the replacement evaluator honestly in the original record.
        for node in ast.walk(rollout):
            if isinstance(node, ast.keyword) and node.arg == 'native_success_definition':
                node.value = ast.Constant('original Toolathlon evaluator; separate from frozen SelfJudge')
        namespace = {**vars(self.io), **vars(self.helpers), **vars(self.reward),
            'json': json, 'asyncio': asyncio, 'journal': journal, 'plan': plan,
            'role_manifest': role_manifest, 'tokenizer': tokenizer, 'clients': clients,
            'executor': executor, 'episode_sem': asyncio.Semaphore(32), 'domain': 'Toolathlon',
            'prompts': self.prompts, 'correctness_prompt': correctness_prompt,
            'selfjudge_schema': None, 'selfjudge_kind': 'alf_boolean_v1',
            'render_curator': self.protocol.render_native, 'grounded_adapter': None,
            'CS': {'type': 'object', 'additionalProperties': False,
                   'required': ['VALID', 'ISSUES', 'EXPLANATION'], 'properties': {
                       'VALID': {'type': 'boolean'}, 'ISSUES': {'type': 'array', 'items': {'type': 'string'}},
                       'EXPLANATION': {'type': 'string'}}}}
        # Retain module-local import mapping used by group_reward_masks.
        namespace['__builtins__'] = self.helpers.__dict__['__builtins__']
        output = ast.fix_missing_locations(ast.Module(body=list(functions.values()), type_ignores=[]))
        exec(compile(output, str(path), 'exec'), namespace)
        return namespace['rollout'], namespace['finalize_group']
