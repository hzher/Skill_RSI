"""Own a native CL-Bench rollout while exposing one instance at a time to MCP."""
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from console_common import atomic, digest


class FamilyEnvironment:
    def __init__(self, config, family, directory):
        self.config = config
        self.name = family
        self.directory = directory
        self.task = None
        self.query = None
        self.terminal = False
        self.receipts = {}
        self.owned = []
        self.episode = None
        self.prior_usage = {}
        self.patches = ExitStack()

    def start(self, indices):
        from src.registry import get_task_class
        from skillrl.interactive import native_task_run_args
        def track(identifier):
            self.owned.append(identifier)
            atomic(self.directory/'owned-containers.json',self.owned)

        if self.name in ('sales_prediction', 'codebase_adaptation'):
            from minisweagent.environments import docker as backend
            factory = backend.DockerEnvironment

            def launch(**kwargs):
                kwargs['run_args'] = native_task_run_args(kwargs['run_args'], self.config['network'])
                env = factory(**kwargs)
                track(env.container_id)
                return env

            self.patches.enter_context(patch.object(backend, 'DockerEnvironment', launch))
        if self.name=='codebase_adaptation':
            from src.tasks.codebase_adaptation import generic_runtime
            create_verifier = generic_runtime._start_generic_container

            def verifier(*args,**kwargs):
                identifier=create_verifier(*args,**kwargs)
                track(identifier)
                return identifier

            self.patches.enter_context(patch.object(generic_runtime,'_start_generic_container',verifier))
        self.task = get_task_class(self.name)(schedule='default')
        self.task.prepare_run(0)
        if self.name in ('sales_prediction', 'codebase_adaptation'):
            original = self.task._next_query

            def next_query():
                # Both native tasks rotate or replace the workspace while building
                # the next query, within step(). Freeze the outgoing one first.
                if self.episode is not None and (getattr(self.task, '_new_issue_pending', False)
                                                  or getattr(self.task, '_new_instance_pending', False)):
                    self.episode.capture()
                return original()

            self.patches.enter_context(patch.object(self.task, '_next_query', next_query))
        if self.name == 'database_exploration':
            swap = self.task._swap_to_drifted_db

            def swap_after_capture():
                if self.episode is not None:
                    self.episode.capture()
                return swap()

            self.patches.enter_context(patch.object(self.task, '_swap_to_drifted_db', swap_after_capture))
        # reset() builds the canonical list and selects the complete native schedule.
        # A selected prefix must exercise that same continuous task, not N baselines.
        self.query = self.task.reset()
        if len(indices) > len(self.task.instances) or indices != list(range(len(indices))):
            raise ValueError('Stateful plans require a canonical prefix per family')
        self.collect()
        self.terminal = bool((self.query.metadata or {}).get('done'))

    def collect(self, latest=None):
        items = list(self.task.get_instance_outcomes())
        if latest is not None:
            items.append(latest)
        for outcome in items:
            value = asdict(outcome)
            idx = value['instance_index']
            if idx in self.receipts and self.receipts[idx] != value:
                raise RuntimeError('Native verdict changed for canonical instance '+str(idx))
            self.receipts[idx] = value

    @property
    def container(self):
        return getattr(getattr(self.task, '_env', None), 'container_id', None)

    @property
    def cwd(self):
        env = getattr(self.task, '_env', None)
        return getattr(getattr(env, 'config', None), 'cwd', None) or '/workspace'

    def evaluate(self):
        actual = asdict(self.task.evaluate())
        atomic(self.directory/'native-objective.json', actual)
        official = {o['instance_index']: o for o in actual['instance_outcomes']}
        if len(official) != len(actual['instance_outcomes']) or official != self.receipts:
            raise RuntimeError('Final native evaluator disagrees with per-instance receipts')
        return digest(self.directory/'native-objective.json')

    def close(self):
        from skillrl.clbench import CLBenchTask
        try:
            if self.task is not None:
                CLBenchTask.close(self)
        finally:
            self.patches.close()


class Episode:
    def __init__(self, family, descriptor, path):
        self.family = family
        self.index = descriptor['canonical_index']
        self.path = path
        self.actions = 0
        self.auto = self.index in family.receipts
        if self.auto:
            self.instance_id = family.receipts[self.index]['instance_id']
        else:
            if family.terminal or family.query is None or family.query.instance_index != self.index:
                raise RuntimeError('Official query order differs from frozen plan at '+descriptor['id'])
            self.instance_id = family.query.instance_id
        family.episode = self

    @property
    def done(self):
        return self.index in self.family.receipts

    def status(self):
        query = self.family.query
        return {'done': self.done, 'cwd': self.family.cwd, 'instance_id': self.instance_id,
                'instance_index': self.index, 'query': None if self.done else query.prompt,
                'response_schema': None if self.done else query.response_schema.model_json_schema()}

    def act(self, action):
        from src.interface import Response
        from skillrl.interactive import ActionValidationError
        if self.done:
            raise ActionValidationError('This instance has ended; finish this turn.')
        try:
            validated = self.family.query.response_schema.model_validate(action)
        except ValueError as error:
            raise ActionValidationError(str(error)) from error
        result = self.family.task.step(Response(action=validated))
        self.actions += 1
        self.family.collect(result.instance_outcome)
        self.family.query = result.next_query
        self.family.terminal = result.done
        if not self.done and (result.done or result.next_query is None or result.next_query.instance_index != self.index):
            raise RuntimeError('Native transition advanced without a matching per-instance outcome')
        return {'done': self.done, 'action': validated.model_dump(mode='json'),
                'observation': asdict(result.observation), 'current': self.status()}

    def capture(self):
        from skillrl.clbench import capture_workspace
        if (self.path/'workspace-capture.json').exists():
            return
        if self.family.container and not self.auto:
            if not capture_workspace(self.family.container, self.family.cwd, self.path):
                raise RuntimeError('Failed to freeze the native workspace')
        else:
            atomic(self.path/'workspace-capture.json', {'kind':'official_in_memory_task', 'filesystem_workspace':False,
                   'public_outputs':['final-task.json','tools/','mcp.jsonl'], 'captured':True})
        db = getattr(self.family.task, '_temp_db_path', None)
        if db and Path(db).is_file():
            atomic(self.path/'database-state.json', {'sha256':digest(db),'bytes':Path(db).stat().st_size,
                   'snapshot_kind':'state_digest','full_state_archived':False,'evaluation_mode':'original_live_family_task'})
