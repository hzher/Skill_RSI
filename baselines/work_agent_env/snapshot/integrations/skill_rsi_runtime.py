"""Durable, idempotent external-Skill evaluation over the existing NeMo executor."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import os
import math
from pathlib import Path
import sqlite3
import uuid

from integrations.model_task_executor import execute_task, redact, stamp
from integrations.skill_rsi_contract import (RSIError, available_tasks, descriptor_id, executor_descriptor,
                                             skill_version, validate_request)
from training.skill_contract import canonical
from workenv.core import no_links, save
from integrations.skill_rsi_locks import rsi_lock

MAX_JOBS = 256


class EvaluationStore:
    """Execution intents and terminal results, separate from the environment's own ledger."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / '.runtime/skill-rsi'
        no_links(self.directory, self.root)
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / 'evaluations.sqlite3'
        no_links(self.path, self.root)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS evaluations (
                request_id TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, executor_id TEXT NOT NULL,
                state TEXT NOT NULL, request_json TEXT NOT NULL, result_json TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        no_links(self.path, self.root)
        if self.path.exists() and self.path.stat().st_size > 64 * 1024 * 1024:
            raise RSIError('evaluation_store_size_limit')
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def reserve(self, request, executor_id):
        digest = hashlib.sha256(canonical({'request': request, 'executor_id': executor_id})).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM evaluations WHERE request_id=?', (request['request_id'],)).fetchone()
            if row:
                if row['request_sha256'] != digest:
                    raise RSIError('request_id_conflict')
                return False, self._view(row)
            if db.execute('SELECT COUNT(*) FROM evaluations').fetchone()[0] >= MAX_JOBS:
                raise RSIError('evaluation_store_job_limit')
            now = stamp()
            db.execute('INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?)',
                       (request['request_id'], digest, executor_id, 'accepted', canonical(request).decode(), None, now, now))
        return True, self.get(request['request_id'])

    @staticmethod
    def _view(row):
        return {'request_id': row['request_id'], 'request_sha256': row['request_sha256'],
                'executor_id': row['executor_id'], 'state': row['state'], 'created_at': row['created_at'],
                'updated_at': row['updated_at'], 'result': json.loads(row['result_json']) if row['result_json'] else None}

    def get(self, request_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM evaluations WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            raise RSIError('evaluation_not_found')
        return self._view(row)

    def transition(self, request_id, expected, state, result=None):
        with self.connect() as db:
            changed = db.execute('UPDATE evaluations SET state=?,result_json=?,updated_at=? WHERE request_id=? AND state=?',
                (state, canonical(result).decode() if result is not None else None, stamp(), request_id, expected)).rowcount
            if changed != 1:
                raise RSIError('evaluation_state_conflict')

    def unresolved(self, request_id):
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM evaluations WHERE request_id<>? AND state IN ('accepted','running','unknown')",
                              (request_id,)).fetchone()[0] != 0


class SkillRSI:
    def __init__(self, root, settings, *, max_steps=100, wall_seconds=1800, runner=None, preflight=None):
        self.root = Path(root).resolve()
        self.settings = settings
        self.max_steps, self.wall_seconds = max_steps, wall_seconds
        self.tasks = available_tasks(self.root)
        self.descriptor = executor_descriptor(self.root, settings, max_steps=max_steps, wall_seconds=wall_seconds)
        self.executor_id = descriptor_id(self.descriptor)
        self.store = EvaluationStore(self.root)
        snapshot = self.store.directory / 'executors' / self.executor_id
        for name, digest in self.descriptor['source_sha256'].items():
            raw = (self.root / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                raise RSIError('executor_or_environment_changed')
            target = snapshot / 'source' / name
            no_links(target, self.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() != raw:
                raise RSIError('executor_snapshot_conflict')
            if not target.exists():
                target.write_bytes(raw)
        save(snapshot / 'descriptor.json', self.descriptor)
        self.runner = runner or execute_task
        if preflight is None:
            from scripts.run_model_tasks import preflight
        self.preflight = preflight

    def validate(self, request):
        return validate_request(request, self.tasks)

    def assert_frozen(self):
        current = executor_descriptor(self.root, self.settings, max_steps=self.max_steps, wall_seconds=self.wall_seconds)
        if descriptor_id(current) != self.executor_id:
            raise RSIError('executor_or_environment_changed')

    def result_from_report(self, request, report):
        """Shared binding checks for normal completion and explicit, evidence-only recovery."""
        identity = request['request_id']
        expected_skill = skill_version(request['skill']) if request['skill'] is not None else None
        if (report.get('evaluation_scope') != 'skill_rsi' or report.get('skill_sha256') != expected_skill
            or report.get('evaluation_request_id') != identity or report.get('task_id') != request['task_id']
            or report.get('requested_model') != self.settings.model):
            raise RSIError('execution_binding_mismatch')
        from integrations.model_task_executor import EXECUTION_SOURCES
        names = (*EXECUTION_SOURCES,'integrations/skill_rsi_contract.py')
        if (report.get('source_sha256') != {name:self.descriptor['source_sha256'][name] for name in names}
            or report.get('environment_source_sha256') != self.descriptor['environment_source_sha256']):
            raise RSIError('execution_source_binding_mismatch')
        try:
            if uuid.UUID(hex=report['run_id']).hex != report['run_id']:
                raise ValueError()
            if (report['status'] not in ('passed','business_failed','unknown')
                or type(report['closed']) is not bool or type(report['source_unchanged']) is not bool
                or type(report['model_http_requests']) is not int or not 0 <= report['model_http_requests'] <= self.max_steps
                or type(report['elapsed_seconds']) not in (int,float)
                or not math.isfinite(report['elapsed_seconds']) or report['elapsed_seconds'] < 0):
                raise ValueError()
            if report['status'] in ('passed','business_failed'):
                measured = report['measurement']
                if (measured.get('mask_sample') is not False or type(measured.get('reward')) not in (int,float)
                    or measured['reward'] != (1 if report['status']=='passed' else 0)
                    or not report['closed'] or not report['source_unchanged']):
                    raise ValueError()
        except (KeyError,ValueError,TypeError,AttributeError):
            raise RSIError('invalid_execution_result') from None
        return redact({'schema_version':'skill-rsi-result-v1','request_id':identity,'task_id':request['task_id'],
            'executor_id':self.executor_id,'skill_sha256':expected_skill,
            'skill_mode':'external_skill' if expected_skill is not None else 'no_skill_baseline',
            'run_id':report['run_id'],'status':report['status'],'failure_kind':report.get('failure_kind'),
            'measurement':report.get('measurement'),'closed':report['closed'],'source_unchanged':report['source_unchanged'],
            'model_http_requests':report['model_http_requests'],'elapsed_seconds':report['elapsed_seconds'],
            'model_reported_usage':report.get('model_reported_usage',{}),'trace_ref':'model-task:'+report['run_id'],
            'training_eligible':False,'training_updates':0,'official_score_available':False,
            'initial_state_equality_attested':False,'sampling_deterministic':False},self.settings.api_key)

    def evaluate(self, request):
        request, created, job = self.prepare(request)
        return self._execute_reserved(request) if created else job

    def prepare(self, request):
        """Durably reserve before dispatch. Only the creator may dispatch this intent."""
        request = self.validate(request)
        self.assert_frozen()
        created, job = self.store.reserve(request, self.executor_id)
        return request, created, job

    def _execute_reserved(self, request):
        # Internal dispatcher only. Public retries use evaluate/prepare and never resume intents.
        _, job = self.store.reserve(request, self.executor_id)
        if job['state'] != 'accepted':
            return job
        identity = request['request_id']
        started = False
        try:
            with rsi_lock(self,'campaign','skill_rsi_evaluation',request_id=identity):
                if self.store.unresolved(identity):
                    raise RSIError('previous_evaluation_unresolved')
                self.preflight()
                self.assert_frozen()
                self.store.transition(identity, 'accepted', 'running')
                started = True
                report = self.runner(self.root, request['task_id'], self.settings,
                    max_steps=self.max_steps, wall_seconds=self.wall_seconds,
                    skill=deepcopy(request['skill']), evaluation_scope='skill_rsi', evaluation_request_id=identity)
                private = self.store.directory / (identity + '.execution.json')
                save(private, {'executor': self.descriptor, 'execution': report})
                os.chmod(private, 0o600)
                self.assert_frozen()
            result = self.result_from_report(request,report)
            public = self.root / 'evidence/skill_rsi/runs' / (identity + '.json')
            no_links(public,self.root)
            save(public, result)
            state = 'completed' if report['closed'] is True and report['source_unchanged'] is True else 'unknown'
            self.store.transition(identity, 'running', state, result)
        except Exception as exc:
            # Unknown includes lost writes, process interruptions and adapter failures. Nothing is retried.
            code = exc.code if isinstance(exc, RSIError) else 'runtime_not_ready' if not started else 'execution_unknown'
            self.store.transition(identity, 'running' if started else 'accepted', 'unknown' if started else 'rejected',
                                  {'error': code, 'training_eligible': False})
        return self.store.get(identity)
