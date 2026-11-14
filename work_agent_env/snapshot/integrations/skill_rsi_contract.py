"""External Skill evaluation contracts; no model calls, task answers or training admission."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

from training.skill_contract import canonical, validate_skill

MAX_REQUEST_BYTES = 128 * 1024
MAX_SKILL_BYTES = 64 * 1024
EVALUATION_SCHEMA = 'skill-rsi-evaluation-v1'


class RSIError(ValueError):
    """Only fixed codes may cross the external API boundary."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def checked_skill(value):
    try:
        skill = deepcopy(validate_skill(deepcopy(value)))
        if len(canonical(skill)) > MAX_SKILL_BYTES:
            raise RSIError('skill_size_limit')
        return skill
    except RSIError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RSIError('invalid_skill') from None


def skill_version(value):
    return hashlib.sha256(canonical(checked_skill(value))).hexdigest()


def validate_request(value, task_ids):
    if not isinstance(value, dict) or set(value) != {'schema_version', 'request_id', 'task_id', 'skill'}:
        raise RSIError('invalid_evaluation_fields')
    if value['schema_version'] != EVALUATION_SCHEMA:
        raise RSIError('unsupported_evaluation_schema')
    try:
        identity = value['request_id']
        if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise RSIError('invalid_request_id') from None
    if not isinstance(value['task_id'], str) or value['task_id'] not in task_ids:
        raise RSIError('task_not_available')
    skill = checked_skill(value['skill']) if value['skill'] is not None else None
    request = {**value, 'skill': skill}
    try:
        if len(canonical(request)) > MAX_REQUEST_BYTES:
            raise RSIError('request_size_limit')
    except (TypeError, UnicodeError, RecursionError):
        raise RSIError('invalid_evaluation_json') from None
    return deepcopy(request)


def available_tasks(root):
    data = json.loads((Path(root) / 'configs/task_adapter_profiles.json').read_text(encoding='utf-8'))
    return sorted({p['task_id'] for p in data['profiles'].values() if 'isolated' in p['resource_backends']})


def restrict_tools(tools, mapping, skill):
    """Exact capability intersection. Verification remains an environment control."""
    granted = set(skill['allowed_tools'])
    if granted - set(mapping):
        raise RSIError('skill_tool_not_available')
    if 'write_workbook_from_results' in granted and 'write_workbook' not in granted:
        raise RSIError('reference_writer_requires_workbook_permission')
    granted.add('verify_task')
    return ([deepcopy(t) for t in tools if t['name'] in granted],
            {name: value for name, value in mapping.items() if name in granted})


def executor_descriptor(root, settings, *, max_steps, wall_seconds):
    from integrations.model_task_executor import EXECUTION_SOURCES, MAX_MODEL_TOOL_CALLS, MAX_NEMO_OPERATIONS
    from integrations.runtime_identity import expected_identity
    if type(max_steps) is not int or not 1 <= max_steps <= 100:
        raise RSIError('invalid_step_budget')
    if type(wall_seconds) is not int or not 30 <= wall_seconds <= 1800:
        raise RSIError('invalid_wall_budget')
    root = Path(root)
    names = (*EXECUTION_SOURCES, 'integrations/skill_rsi_contract.py', 'integrations/skill_rsi_runtime.py',
             'integrations/skill_rsi_locks.py')
    return {'schema_version': 'skill-rsi-executor-v1', 'protocol': settings.protocol,
            'requested_model': settings.model, 'endpoint_sha256': hashlib.sha256(settings.base_url.encode()).hexdigest(),
            'max_output_tokens': settings.max_tokens, 'reasoning_effort': settings.reasoning_effort,
            'timeout_seconds': settings.timeout_seconds, 'chat_token_field': settings.chat_token_field,
            'anthropic_version': settings.anthropic_version, 'max_steps': max_steps, 'wall_seconds': wall_seconds,
            'max_tool_calls_per_response': MAX_MODEL_TOOL_CALLS, 'max_nemo_operations': MAX_NEMO_OPERATIONS,
            'source_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names},
            'environment_source_sha256': expected_identity(root),
            'sampling_deterministic': False, 'underlying_model_revision_attested': False}


def descriptor_id(descriptor):
    return hashlib.sha256(canonical(descriptor)).hexdigest()
