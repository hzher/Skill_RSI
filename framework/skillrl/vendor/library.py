"""Zero-to-many model-authored deltas committed under one parent-state lock."""
import fcntl
import hashlib
import json
import re
from pathlib import Path

from .common import canonical, file_sha, immutable, load
from .library_base import InvalidOperations, SkillLibrary, parse_skill


def quality(content):
    fields, body = parse_skill(content)
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,63}', fields['name']):
        raise InvalidOperations('Use a descriptive lowercase hyphenated skill name')
    if len(content) > 3500 or len(fields['description']) > 350:
        raise InvalidOperations('Skill or catalog description exceeds declared envelope')
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) > 8 and len(set(lines)) / len(lines) < .75:
        raise InvalidOperations('Repeated procedure lines require deduplication')
    return fields


def validate_dependencies(bodies):
    graph = {}
    for name, body in bodies.items():
        lines = [line[len('组合依赖 '):] for line in body.splitlines() if line.startswith('组合依赖 ')]
        dependencies = json.loads(lines[0]) if lines else []
        if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies) or not set(dependencies) <= set(bodies):
            raise InvalidOperations('Composition dependency is absent or archived')
        graph[name] = dependencies
    visited, visiting = set(), set()

    def visit(name):
        if name in visiting:
            raise InvalidOperations('Composition cycle rejected')
        if name in visited:
            return
        visiting.add(name)
        for dependency in graph[name]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in graph:
        visit(name)


class DeltaLibrary(SkillLibrary):
    """Reuse stable IDs/blobs while validating semantic merge/split/archive deltas."""

    def commit(self, stage, proposal, expected_parent, read_names, read_events):
        with (self.root / 'DELTA_LOCK').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            transaction = self.root / 'transactions' / (hashlib.sha256(stage.encode()).hexdigest() + '.json')
            recorded = {'proposal': proposal, 'expected_parent': expected_parent,
                        'read_names': sorted(read_names), 'read_events': sorted(read_events)}
            if transaction.exists():
                if load(transaction) != recorded:
                    raise InvalidOperations('Conflicting persisted transaction')
            state = self.state()
            if stage in state['processed']:
                if not transaction.exists():
                    raise InvalidOperations('Processed transaction lacks original model proposal')
                result = state['processed'][stage]
                immutable(self.root / 'commits' / (hashlib.sha256(stage.encode()).hexdigest() + '.json'), canonical(result))
                return result
            if file_sha(self.root / 'state.json') != expected_parent:
                raise InvalidOperations('Parent changed after evidence preparation')
            operations = self.validate(proposal, state, read_names, read_events)
            immutable(transaction, canonical(recorded))
            return self.apply(stage, operations)

    def validate(self, proposal, state, read_names, read_events):
        if not isinstance(proposal, dict) or set(proposal) != {'reason', 'operations'}:
            raise InvalidOperations('Exact reason/operations transaction required')
        if not isinstance(proposal['reason'], str) or not proposal['reason'].strip():
            raise InvalidOperations('Evidence-based transaction reason required')
        deltas = proposal['operations']
        if not isinstance(deltas, list) or len(deltas) > 8:
            raise InvalidOperations('Zero to eight deltas, with no generation quota')
        active = state['active']
        names = set(active)
        touched = set()
        operations = []
        bodies = {name: (self.root / 'raw_versions' / (entry['raw_sha256'] + '.md')).read_text()
                  for name, entry in active.items()}
        for delta in deltas:
            if not isinstance(delta, dict) or set(delta) != {'operation', 'parents', 'skills', 'evidence_ids', 'reason'}:
                raise InvalidOperations('Exact delta fields required')
            kind = delta['operation']
            parents, children, citations = delta['parents'], delta['skills'], delta['evidence_ids']
            if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):
                raise InvalidOperations('Parent names must be a list')
            if len(set(parents)) != len(parents) or not set(parents) <= names or set(parents) & touched:
                raise InvalidOperations('Parent absent, duplicate or already changed')
            if not set(parents) <= set(read_names):
                raise InvalidOperations('Every changed parent needs an actual full native Read')
            if not isinstance(citations, list) or not citations or not all(isinstance(c, str) for c in citations) or not set(citations) <= set(read_events):
                raise InvalidOperations('Cite actual full evidence Reads')
            if not isinstance(delta['reason'], str) or not delta['reason'].strip() or not isinstance(children, list):
                raise InvalidOperations('Reason and child list required')
            shape = {'add': (len(parents) == 0 and len(children) == 1),
                     'update': (len(parents) == 1 and len(children) == 1),
                     'merge': (len(parents) >= 2 and len(children) == 1),
                     'split': (len(parents) == 1 and 2 <= len(children) <= 4),
                     'archive': (len(parents) >= 1 and not children)}
            if not shape.get(kind):
                raise InvalidOperations('Invalid add/update/merge/split/archive shape')
            parsed = []
            for child in children:
                if not isinstance(child, dict) or set(child) != {'content'}:
                    raise InvalidOperations('Child must contain actual model-authored Markdown content')
                fields = quality(child['content'])
                name = fields['name']
                if name in touched or name in names - set(parents) or name in [n for n, _ in parsed]:
                    raise InvalidOperations('Child name collision')
                if child['content'] in bodies.values():
                    raise InvalidOperations('Unchanged/duplicate body requires no-op')
                parsed.append((name, child['content']))
            if kind == 'update' and parsed[0][0] != parents[0]:
                raise InvalidOperations('UPDATE retains exact stable parent name')
            if kind == 'update':
                operations.append({'name': 'skill_update', 'arguments': {'skill_name': parents[0], 'new_content': parsed[0][1]}})
            else:
                for name in parents:
                    operations.append({'name': 'skill_delete', 'arguments': {'skill_name': name}})
                for name, body in parsed:
                    operations.append({'name': 'new_skill_insert', 'arguments': {'skill_name': name, 'content': body}})
            touched.update(parents)
            touched.update(name for name, _ in parsed)
            names.difference_update(parents)
            names.update(name for name, _ in parsed)
            for name in parents:
                bodies.pop(name, None)
            bodies.update(parsed)
        if len(operations) > 8 or len(names) > 24:
            raise InvalidOperations('Atomic native operation cap8 / active catalog cap24')
        validate_dependencies(bodies)
        return operations
