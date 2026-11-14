"""Serial, durable observations of candidate Skills; never automatically selects a winner."""
from copy import deepcopy
import hashlib
from pathlib import Path
import uuid

from integrations.skill_rsi_contract import MAX_REQUEST_BYTES, RSIError, skill_version
from integrations.model_task_executor import stamp
from training.skill_contract import canonical
from workenv.core import save
from workenv.leases import owned_lock


class SkillComparison:
    def __init__(self, service):
        self.service = service
        self.source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        with service.store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS comparisons (
                request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL,
                request_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL)''')

    def validate(self, request):
        if (not isinstance(request, dict)
            or set(request) != {'schema_version','request_id','task_id','candidates'}
            or request['schema_version'] != 'skill-rsi-comparison-v1'
            or not isinstance(request['candidates'], list) or not 2 <= len(request['candidates']) <= 4):
            raise RSIError('invalid_comparison_request')
        candidates = []
        for skill in request['candidates']:
            body = self.service.validate({'schema_version':'skill-rsi-evaluation-v1',
                'request_id':request['request_id'], 'task_id':request['task_id'], 'skill':skill})
            candidates.append(body['skill'])
        versions = [skill_version(s) if s is not None else None for s in candidates]
        if len(set(versions)) != len(versions):
            raise RSIError('duplicate_comparison_candidate')
        result = {**request, 'candidates':candidates}
        if len(canonical(result)) > MAX_REQUEST_BYTES:
            raise RSIError('request_size_limit')
        return result

    def get(self, identity):
        import json
        with self.service.store.connect() as db:
            row = db.execute('SELECT * FROM comparisons WHERE request_id=?',(identity,)).fetchone()
        if row is None:
            raise RSIError('comparison_not_found')
        return {'request_id':row['request_id'], 'state':row['state'], 'created_at':row['created_at'],
                'result':json.loads(row['result_json']) if row['result_json'] else None}

    def evaluate(self, request):
        request = self.validate(request)
        self.service.assert_frozen()
        digest = hashlib.sha256(canonical({'request':request, 'executor_id':self.service.executor_id,
                                            'comparison_source_sha256':self.source_sha256})).hexdigest()
        identity = request['request_id']
        with self.service.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT digest FROM comparisons WHERE request_id=?',(identity,)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise RSIError('request_id_conflict')
            else:
                if db.execute('SELECT COUNT(*) FROM comparisons').fetchone()[0] >= 64:
                    raise RSIError('comparison_store_limit')
                db.execute('INSERT INTO comparisons VALUES(?,?,?,?,?,?)',
                           (identity,digest,'running',canonical(request).decode(),None,stamp()))
        if previous:
            return self.get(identity)  # Includes interrupted work: no automatic continuation.
        arms, state, error = [], 'completed', None
        try:
            with owned_lock(self.service.root/'.runtime/skill-rsi-comparison.lock',self.service.root,'skill_rsi_comparison'):
                for index, skill in enumerate(request['candidates']):
                    arm_id = str(uuid.uuid5(uuid.UUID(identity), 'candidate:'+str(index)))
                    job = self.service.evaluate({'schema_version':'skill-rsi-evaluation-v1', 'request_id':arm_id,
                        'task_id':request['task_id'], 'skill':deepcopy(skill)})
                    arms.append(job)
                    result = job.get('result') or {}
                    if (job['state'] != 'completed' or result.get('status') not in ('passed','business_failed')
                        or result.get('closed') is not True or result.get('source_unchanged') is not True):
                        state, error = 'unknown', 'comparison_arm_unresolved'
                        break
        except Exception as exc:
            state, error = 'unknown', exc.code if isinstance(exc,RSIError) else 'comparison_interrupted'
        result = {'schema_version':'skill-rsi-comparison-result-v1', 'request_id':identity,
            'task_id':request['task_id'], 'executor_id':self.service.executor_id,
            'comparison_source_sha256':self.source_sha256, 'arms':arms, 'error':error,
            'planned_arms':len(request['candidates']), 'observed_arms':len(arms),
            'initial_state_equality_attested':False, 'sampling_deterministic':False,
            'selection_eligible':False, 'winner':None, 'training_eligible':False,
            'interpretation':'observations_only_initial_state_and_model_revision_not_attested'}
        save(self.service.root/'evidence/skill_rsi/comparisons'/(identity+'.json'),result)
        with self.service.store.connect() as db:
            db.execute('UPDATE comparisons SET state=?, result_json=? WHERE request_id=?',
                       (state,canonical(result).decode(),identity))
        return self.get(identity)
