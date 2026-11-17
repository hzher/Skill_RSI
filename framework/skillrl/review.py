"""Two-stage, version-bound observational review with one explicit output contract."""
from pathlib import Path
import json
from .models import text_content
from .storage import atomic, begin, seal

FINDING = {'use_state': 'not_used|read_only|used|uncertain', 'event_indices': ['integer indices'],
           'reason': 'string', 'confidence': 'number 0..1', 'uncertainties': ['strings']}
EFFECT = {'local_score': 'integer -2..2 or null', 'overall_score': 'integer -2..2 or null',
          'event_indices': ['integer indices'], 'counterevidence_indices': ['integer indices'],
          'reason': 'string', 'alternative_explanations': ['strings'], 'confidence': 'number 0..1',
          'uncertainties': ['strings']}

def indices(value, count):
    if not isinstance(value, list) or len(set(value)) != len(value) or any(type(i) is not int or not 0 <= i < count for i in value):
        raise ValueError('Invalid event indices')

def strings(value):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError('Expected a list of strings')

def valid_common(value, contract, count):
    if not isinstance(value, dict) or set(value) != set(contract) or not isinstance(value['reason'], str):
        raise ValueError('Review must have the exact declared fields and types')
    indices(value['event_indices'], count)
    strings(value['uncertainties'])
    confidence = value['confidence']
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError('Invalid confidence')

class Reviewer:
    def __init__(self, model, *, action_tools=None):
        self.model = model
        self.action_tools = tuple(action_tools or ['Write', 'Edit', 'Bash'])
        if not self.action_tools or not all(isinstance(name, str) and name for name in self.action_tools):
            raise ValueError('Review action tools must be explicit names')

    def settings(self):
        return {'model': self.model.settings(), 'action_tools': list(self.action_tools)}

    def run(self, task, execution, snapshot, exposure, outcome, path):
        path = Path(path)
        cached = begin(path, {'task_id': task.id, 'versions': snapshot, 'exposure': exposure,
                              'objective': outcome, 'settings': self.settings()})
        if cached is not None:
            return cached
        rows = []
        events = execution['events']
        for version, proof in zip(snapshot, exposure, strict=True):
            row = {key: version[key] for key in ('skill_id', 'name', 'raw_sha256')}
            row.update(body_proven=proof['body_proven'], use_state=None, local_score=None,
                       overall_score=None, training_credit=None, causal_reward=None)
            if not proof['body_proven']:
                row['status'] = 'unverified_body'
                rows.append(row)
                continue
            unit = path / version['skill_id'].split(':')[-1]
            actions = {index for item in proof['proof'] for index in item['subsequent_actions']}
            context = {'task': task.public(), 'target': version, 'exposure': proof, 'events': events,
                       'alternatives': [{key: other[key] for key in ('name', 'skill_id', 'description')}
                                        for other in snapshot if other['skill_id'] != version['skill_id']]}
            finding_prompt = ('Judge observed adoption only. Return one JSON object with exactly this shape ' + json.dumps(FINDING) +
                '. Use actual event indices. used requires a successful subsequent business action from ' + str(list(self.action_tools)) + ' after proven full body exposure. '
                'Self-report and overlap with the task rules alone are insufficient. No causal claims.\n' + json.dumps(context, ensure_ascii=False))
            try:
                response = self.model.call([{'role': 'user', 'content': finding_prompt}], [], unit / 'finding')
                finding = json.loads(text_content(response['message']))
                atomic(unit / 'finding-candidate.json', finding)
                valid_common(finding, FINDING, len(events))
                if finding['use_state'] not in ('not_used', 'read_only', 'used', 'uncertain'):
                    raise ValueError('Unknown adoption state')
                if finding['use_state'] == 'used' and not actions.intersection(finding['event_indices']):
                    raise ValueError('used lacks a subsequent business action')
                row.update(use_state=finding['use_state'], finding=finding, status='finding_complete')
                if finding['use_state'] == 'used':
                    effect_context = {**context, 'objective': outcome, 'finding': finding}
                    effect_prompt = ('Judge this skill version on this recorded trajectory. Return one JSON object exactly matching ' + json.dumps(EFFECT) +
                        '. alternative_explanations and uncertainties are arrays of strings. Scores are integers -2..2 or null. '
                        'Use null for unknown benefit or harm. Cite subsequent business actions. '
                        'Account for other skills and public task instructions. Scores are observational and nonadditive.\n' + json.dumps(effect_context, ensure_ascii=False))
                    response = self.model.call([{'role': 'user', 'content': effect_prompt}], [], unit / 'effect')
                    effect = json.loads(text_content(response['message']))
                    atomic(unit / 'effect-candidate.json', effect)
                    valid_common(effect, EFFECT, len(events))
                    strings(effect['alternative_explanations'])
                    indices(effect['counterevidence_indices'], len(events))
                    for field in ('local_score', 'overall_score'):
                        score = effect[field]
                        if score is not None and (type(score) is not int or score not in range(-2, 3)):
                            raise ValueError('Invalid score')
                    if any(effect[key] is not None for key in ('local_score', 'overall_score')):
                        if not actions.intersection(effect['event_indices']) or not effect['reason'] or not effect['alternative_explanations']:
                            raise ValueError('Numeric attribution lacks action evidence or alternatives')
                    credit = effect['overall_score']
                    if outcome['score'] is None:
                        credit = None
                    elif outcome['score'] == 0 and credit is not None:
                        credit = min(0, credit)
                    row.update(status='review_complete', effect=effect, local_score=effect['local_score'],
                               overall_score=effect['overall_score'], training_credit=credit)
            except Exception as error:
                row.update(status='invalid_or_unresolved_review', local_score=None, overall_score=None,
                           training_credit=None, error=type(error).__name__ + ': ' + str(error)[:500])
            rows.append(row)
        return seal(path, {'task_id': task.id, 'skills': rows, 'task_reward': outcome['score'],
                           'nonadditive': True, 'causal_reward': None})
