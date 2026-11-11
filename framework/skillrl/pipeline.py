"""Execution -> sealed public experience -> curation; evaluation progresses independently."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
from .agents import exposures
from .storage import atomic, begin, seal, writer, load, files, canonical, file_sha, copy_public, PendingStage
from .vendor.library import DeltaLibrary

class Pipeline:
    def __init__(self, root, benchmark, executor, curator, reviewer, *, sequence='skillrl'):
        self.root = Path(root).resolve()
        self.benchmark, self.executor, self.curator, self.reviewer = benchmark, executor, curator, reviewer
        self.sequence = sequence

    def snapshot(self, library):
        from .vendor.library_base import parse_skill
        snapshot = []
        for entry in library.state()['active'].values():
            content = (library.root / 'raw_versions' / (entry['raw_sha256'] + '.md')).read_text()
            fields, _ = parse_skill(content)
            from .vendor.library import quality
            quality(content)
            if fields["name"] != entry["name"]:
                raise ValueError("Active skill name differs from its stored body")
            snapshot.append({**entry, 'description': fields['description'], 'content': content})
        return snapshot

    def evaluate(self, task, execution, public_workspace):
        path = self.root / 'evaluation' / task.id / 'objective'
        cached = begin(path, {'task': task.public(), 'public_artifacts': files(public_workspace)})
        if cached is not None:
            return cached
        # Native verifiers may mutate their copy, never the curator's sealed public view.
        target = path / 'workspace'
        copy_public(public_workspace, target)
        try:
            from .benchmarks import validate_outcome
            outcome = validate_outcome(self.benchmark.evaluate(task, target, execution))
        except Exception as error:
            outcome = {'score': None, 'details': {'status': 'evaluation_error', 'error_type': type(error).__name__}}
        return seal(path, outcome)

    def run(self):
        with writer(self.root), ThreadPoolExecutor(max_workers=2) as grading:
            library = DeltaLibrary(self.root / 'library', self.sequence)
            tasks = self.benchmark.tasks()
            from .storage import identifier
            task_ids = [identifier(task.id) for task in tasks]
            if not task_ids or len(task_ids) != len(set(task_ids)):
                raise ValueError("Nonempty unique stable task IDs required")
            plan = {'tasks': [{**task.public(), 'input_files': files(task.public_dir)} for task in tasks],
                    'executor': self.executor.settings(), 'curator': self.curator.settings(),
                    'reviewer': self.reviewer.settings(), 'sequence': self.sequence,
                    'benchmark': self.benchmark.configuration() if hasattr(self.benchmark, 'configuration') else type(self.benchmark).__name__,
                    'runtime_source_sha256': {str(source.relative_to(Path(__file__).parent)): file_sha(source)
                                              for source in sorted(Path(__file__).parent.rglob('*.py'))}}
            from .storage import immutable
            immutable(self.root / 'plan.json', canonical(plan))
            jobs, statuses = [], []
            for task in tasks:
                path = self.root / 'episodes' / task.id
                path.mkdir(parents=True, exist_ok=True)
                try:
                    if (path / 'execution.json').exists():
                        execution = load(path / 'execution.json')
                        snapshot = load(path / 'snapshot.json')
                    else:
                        snapshot = self.snapshot(library)
                        atomic(path / 'snapshot.json', snapshot)
                        actor = path / 'actor'
                        if (actor / 'intent.json').exists() and not (actor / 'result.json').exists():
                            raise PendingStage('Existing actor intent needs reconciliation')
                        workspace = path / 'workspace'
                        if not workspace.exists():
                            environment = self.benchmark.prepare(task, workspace)
                        else:
                            # A workspace without a sealed actor is not an automatic retry grant.
                            if not (actor / 'result.json').exists():
                                raise PendingStage('Existing workspace has no completed actor')
                            if (path / 'environment.json').exists() and not (path / 'environment-stop.json').exists():
                                raise PendingStage('Docker stop receipt absent; reconcile the original container')
                            environment = None
                        if environment:
                            if hasattr(environment, 'skill_mount'):
                                mount = path / 'skill-mount'
                                mount.mkdir(exist_ok=True)
                                for entry in snapshot:
                                    (mount / (entry['name'] + '.md')).write_text(entry['content'])
                                environment.skill_mount = mount
                            if hasattr(environment, 'cli_mount'):
                                environment.cli_mount = getattr(self.executor, 'cli_host_path', None)
                            try:
                                environment.start()
                                execution = self.executor.execute(task, environment, snapshot, actor)
                            finally:
                                environment.close()
                        else:
                            execution = load(actor / 'result.json')
                        if not execution.get('complete'):
                            raise PendingStage('Execution has no complete transcript')
                        copy_public(workspace, path / 'public-artifacts')
                        atomic(path / 'execution.json', execution)
                        atomic(path / 'artifact-manifest.json', files(path / 'public-artifacts'))
                        if environment and hasattr(environment, 'retire'):
                            environment.retire()
                            execution['environment']['retired_after_archive'] = True
                            atomic(path / 'execution.json', execution)
                    if files(path / 'public-artifacts') != load(path / 'artifact-manifest.json'):
                        raise ValueError('Sealed public workspace changed')
                    exposure = exposures(execution, snapshot, path / 'actor', self.reviewer.action_tools)
                    atomic(path / 'exposure.json', exposure)
                    # Build the immutable observation before any verifier starts.
                    from .curation import observation
                    atomic(path / 'curation-observation.json', observation(task, execution, snapshot, path / 'public-artifacts'))
                    jobs.append((task, execution, snapshot, exposure,
                                 grading.submit(self.evaluate, task, execution, path / 'public-artifacts')))
                    stage = path / 'curator'
                    processed = library.state()['processed'].get('curate-' + task.id)
                    if processed and not (stage / 'result.json').exists():
                        # A crash after commit never reissues a model or a transaction.
                        atomic(stage / 'result.json', {'status': 'committed_final_unresolved', 'commit': processed})
                    self.curator.run(task, execution, snapshot, path / 'public-artifacts', library, stage)
                    statuses.append({'task_id': task.id, 'execution': 'complete', 'curation': load(stage / 'result.json')['status']})
                except Exception as error:
                    atomic(path / 'hold.json', {'kind': type(error).__name__, 'message': str(error)[:1500]})
                    statuses.append({'task_id': task.id, 'status': 'held', 'reason': type(error).__name__})
            reviews = []
            for task, execution, snapshot, exposure, future in jobs:
                try:
                    outcome = future.result()
                    review = self.reviewer.run(task, execution, snapshot, exposure, outcome,
                                               self.root / 'evaluation' / task.id / 'review')
                    reviews.append(review)
                except Exception as error:
                    atomic(self.root / 'evaluation' / task.id / 'hold.json', {'error_type': type(error).__name__})
            complete = len(statuses) == len(tasks) and all(row.get('execution') == 'complete' for row in statuses)
            complete = complete and len(reviews) == len(tasks) and all(row['curation'] in ('applied', 'no_op') for row in statuses)
            valid_reviews = all(row['status'] != 'invalid_or_unresolved_review' for review in reviews for row in review['skills'])
            complete = complete and valid_reviews
            from .reporting import costs
            report = {'cost': costs(self.root), 'review_valid': valid_reviews, 'task_count': len(tasks), 'complete': complete, 'episodes': statuses,
                      'active_skills': len(library.state()['active']), 'reviews': reviews,
                      'weights_updated': False, 'score_visibility_to_curator': False}
            atomic(self.root / 'report.json', report)
            return report
