"""An auxiliary review hold must not become an unbounded model request."""
import json
from pathlib import Path
from .review import Reviewer
from .storage import begin, seal, file_sha


class BoundedReviewer(Reviewer):
    def __init__(self, model, *, action_tools=None, context_limit=160000):
        super().__init__(model, action_tools=action_tools)
        self.context_limit = context_limit

    def settings(self):
        return {**super().settings(), 'context_limit': self.context_limit,
                'overflow': 'explicit unresolved hold, no model call or invented credit',
                'implementation_sha256': file_sha(Path(__file__))}

    def run(self, task, execution, snapshot, exposure, outcome, path):
        size = len(json.dumps({'task': task.public(), 'events': execution['events'],
                               'versions': snapshot, 'exposure': exposure, 'objective': outcome}, ensure_ascii=False))
        if size <= self.context_limit:
            return super().run(task, execution, snapshot, exposure, outcome, path)
        cached = begin(path, {'task_id': task.id, 'versions': snapshot, 'exposure': exposure,
                              'objective': outcome, 'settings': self.settings()})
        if cached is not None:
            return cached
        rows = []
        for version, proof in zip(snapshot, exposure, strict=True):
            row = {key: version[key] for key in ('skill_id', 'name', 'raw_sha256')}
            row.update(body_proven=proof['body_proven'], use_state=None, local_score=None,
                       overall_score=None, training_credit=None, causal_reward=None,
                       status='invalid_or_unresolved_review' if proof['body_proven'] else 'unverified_body')
            if proof['body_proven']:
                row['error'] = 'ContextBudgetExceeded: auxiliary review retained without attribution'
            rows.append(row)
        return seal(path, {'task_id': task.id, 'skills': rows, 'task_reward': outcome['score'],
                           'nonadditive': True, 'causal_reward': None,
                           'context_hold': {'characters': size, 'limit': self.context_limit,
                                            'model_called': False, 'raw_evidence_preserved': True}})
