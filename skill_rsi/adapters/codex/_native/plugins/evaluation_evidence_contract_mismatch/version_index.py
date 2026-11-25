"""Episode-local index of observed source versions; no authority inferred from titles."""
import json
from pathlib import Path
import re

from ...storage import atomic
from ..common import digest


class SourceVersionIndex:
    version2 = False

    def __init__(self, config, spec):
        self.config, self.spec = config, spec
        self.path = Path(config['events']).with_name('source-version-index.json')

    def catalog(self):
        return []

    def initial_context(self):
        if not re.search(r'\b(version|prior|previous|current|exact|same|latest|saved|standing)\b',
                         self.config.get('public_task', ''), re.I):
            return ''
        return ('\n\nSource version discipline: distinguish a requested historical record from a '
                'current effective value. Read the actual target before selecting a source when it '
                'can carry that distinction. Keep each source\'s fields together. Titles, ordering '
                'and increasing identifiers do not by themselves establish replacement authority. '
                'Use explicit applicability, revision or replacement evidence; otherwise retain the '
                'conflict. Complete independent work without inventing missing authorization.')

    def observe(self, event):
        if event.get('error') or event['name'] not in self.spec['read_tools']:
            return None
        value = event.get('result')
        if not isinstance(value, dict) or value.get('error'):
            return None
        candidates = value.get('sessions')
        if not isinstance(candidates, list):
            candidates = [value] if any(k in value for k in ('note_id', 'article_id', 'raw_sha256')) else []
        previous = json.loads(self.path.read_text()) if self.path.exists() else []
        added = 0
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            source = candidate.get('session_id') or candidate.get('id') or candidate.get('note_id') or candidate.get('article_id') or candidate.get('name')
            if not isinstance(source, str):
                continue
            version = digest(candidate)
            if any(row['source_id'] == source and row['source_sha256'] == version for row in previous):
                continue
            claims = candidate.get('content')
            if claims is None:
                claims = [m.get('content') for m in candidate.get('messages', [])
                          if isinstance(m, dict) and m.get('role') in ('user', 'assistant')]
            encoded = json.dumps(claims, ensure_ascii=False)
            previous.append({'source_id': source, 'title': candidate.get('title'),
                             'source_sha256': version, 'observed_call_id': str(event['call_id']),
                             'claims': claims if len(encoded) <= 2400 else None,
                             'claims_omitted': len(encoded) > 2400,
                             'authority': 'unverified; inspect original result when needed'})
            added += 1
        if not added:
            return None
        previous = previous[-12:]
        atomic(self.path, previous)
        if len(previous) < 2:
            return None
        receipt = {'plugin': self.spec['kind'], 'source_actor': 'framework_plugin',
                   'source_call_id': str(event['call_id']), 'status': 'observed_source_versions_indexed',
                   'persisted': False, 'raw_sha256': digest(value),
                   'information_support': [{'kind': 'observed_source_version_index',
                      'sources': previous, 'boundary': 'These are actual observed alternatives, not an authority ranking. Match the requested source, object, scope and time; preserve whole field groups. A newer title is not replacement evidence.'}]}
        with Path(self.config['plugin_events']).open('a') as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False) + '\n')
        return receipt
