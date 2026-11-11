"""Replaceable dataset, environment and objective evaluation boundaries."""
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import json
import subprocess
from .environments import DirectoryEnvironment, DockerEnvironment
from .storage import identifier, copy_public, contained, files, load

@dataclass(frozen=True)
class Task:
    id: str
    instruction: str
    public_dir: Path

    def public(self):
        return {'id': self.id, 'instruction': self.instruction}

class Benchmark(Protocol):
    def tasks(self) -> list[Task]: ...
    def prepare(self, task: Task, workspace: Path): ...
    def evaluate(self, task: Task, workspace: Path, execution: dict) -> dict: ...


def validate_outcome(outcome):
    if not isinstance(outcome, dict) or set(outcome) != {'score', 'details'}:
        raise ValueError('Objective evaluator must return exactly score and details')
    score = outcome['score']
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float)) or score not in (0, 1)):
        raise ValueError('Task reward is 0, 1 or null')
    if not isinstance(outcome['details'], dict):
        raise ValueError('Objective details must be an object')
    return outcome

class ManifestBenchmark:
    """Public files and private evaluation metadata are configured in separate domains."""
    def __init__(self, manifest, *, image=None, evaluator=None, network='none', user=None, security_profile='hardened'):
        self.manifest = Path(manifest).resolve()
        self.base = self.manifest.parent
        self.image = image
        self.evaluator = evaluator
        self.network = network
        self.user, self.security_profile = user, security_profile
        rows = load(self.manifest)
        if not isinstance(rows, list) or not rows:
            raise ValueError('Nonempty ordered task manifest required')
        self.rows = {}
        for row in rows:
            if set(row) != {'id', 'instruction', 'public_dir'}:
                raise ValueError('Manifest only admits id, instruction and public_dir')
            identifier(row['id'])
            if row['id'] in self.rows or not isinstance(row['instruction'], str) or not row['instruction'].strip():
                raise ValueError('Duplicate ID or missing public instruction')
            self.rows[row['id']] = Task(row['id'], row['instruction'], contained(self.base, row['public_dir']))

    def configuration(self):
        evaluator = self.evaluator.configuration() if hasattr(self.evaluator, 'configuration') else {'class': type(self.evaluator).__name__}
        return {'class': type(self).__name__, 'manifest_sha256': __import__('hashlib').sha256(self.manifest.read_bytes()).hexdigest(),
                'image': self.image, 'images': getattr(self, 'images', {}), 'network': self.network,
                'user': self.user, 'security_profile': self.security_profile, 'evaluator': evaluator}

    def tasks(self):
        return list(self.rows.values())

    def prepare(self, task, workspace):
        copy_public(task.public_dir, workspace)
        if self.image:
            return DockerEnvironment(workspace, self.image, network=self.network, user=self.user, security_profile=self.security_profile)
        return DirectoryEnvironment(workspace)

    def evaluate(self, task, workspace, execution):
        if not self.evaluator:
            return {'score': None, 'details': {'status': 'not_configured'}}
        return validate_outcome(self.evaluator(task, workspace, execution))

class JSONExactEvaluator:
    """Small example evaluator; other benchmarks replace this callable."""
    def __init__(self, expected, output='output/result.json'):
        self.expected = Path(expected).resolve()
        self.output = output

    def configuration(self):
        return {'kind': 'json_exact', 'output': self.output, 'reference_files': files(self.expected)}

    def __call__(self, task, workspace, execution):
        try:
            actual = load(contained(workspace, self.output))
        except (OSError, ValueError):
            return {'score': 0, 'details': {'status': 'missing_or_invalid_output'}}
        expected = load(self.expected / (task.id + '.json'))
        # Canonical JSON distinguishes booleans from numbers and preserves exact contracts.
        equal = json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
        return {'score': int(equal), 'details': {'status': 'passed' if equal else 'mismatch'}}

class CommandEvaluator:
    """Host-side hook to the benchmark's original verifier. Shell interpolation is absent."""
    def __init__(self, argv, *, timeout=900):
        if not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError('Verifier argv must be a list of strings')
        self.argv, self.timeout = argv, timeout

    def configuration(self):
        from .storage import file_sha
        return {'kind': 'command', 'argv': self.argv, 'timeout': self.timeout,
                'files': {arg: file_sha(arg) for arg in self.argv if Path(arg).is_file()}}

    def __call__(self, task, workspace, execution):
        request = {'task_id': task.id, 'workspace': str(Path(workspace).resolve()),
                   'environment': execution.get('environment', {})}
        result = subprocess.run(self.argv, input=json.dumps(request), capture_output=True,
                                text=True, timeout=self.timeout)
        if result.returncode:
            return {'score': None, 'details': {'status': 'verifier_error', 'exit_code': result.returncode}}
        return validate_outcome(json.loads(result.stdout))

class WorkBuddyBenchmark(ManifestBenchmark):
    """Load actual WorkBuddy instruction/workspace exports through an explicit index.

    Each public_dir must be an audited export of the original task's workspace.
    Original Docker image and verifier hook are required. No oracle is copied here.
    """
    def __init__(self, manifest, *, evaluator, image=None, images=None, network='none', user=None, security_profile='hardened'):
        if not (image or images) or not isinstance(evaluator, CommandEvaluator):
            raise ValueError('WorkBuddy requires original task images and a native verifier command')
        super().__init__(manifest, image=image, evaluator=evaluator, network=network, user=user, security_profile=security_profile)
        self.images = images or {}
        if not image and not set(self.rows) <= set(self.images):
            raise ValueError('Original image mapping is incomplete')

    def prepare(self, task, workspace):
        copy_public(task.public_dir, workspace)
        return DockerEnvironment(workspace, self.images.get(task.id, self.image), network=self.network, user=self.user, security_profile=self.security_profile)

    @classmethod
    def from_export(cls, index, **kwargs):
        return cls(index, **kwargs)
