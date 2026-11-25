"""Recover exact public literals; never read a grader or choose hidden vocabulary."""
from datetime import datetime
import json
from pathlib import Path
import re

from ..common import digest

_MONTH = r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
_PATTERNS = [r'`([^`\n]{2,160})`', r'\b[A-Z][A-Za-z_-]*(?:/[A-Z][A-Za-z_-]*){1,5}\b',
             r'\b20\d{2}-\d{2}-\d{2}\b', rf'\b{_MONTH}\s+\d{{1,2}},?\s+20\d{{2}}\b']


def literals(value):
    if isinstance(value, dict):
        texts = [text for child in value.values() for text in literals(child)]
    elif isinstance(value, list):
        texts = [text for child in value for text in literals(child)]
    elif isinstance(value, str):
        texts = []
        for pattern in _PATTERNS:
            for match in re.finditer(pattern, value):
                text = match.group(1) if match.lastindex else match.group(0)
                if text not in texts:
                    texts.append(text)
                for fmt in ('%B %d, %Y', '%B %d %Y'):
                    try:
                        canonical = datetime.strptime(text, fmt).date().isoformat()
                    except ValueError:
                        continue
                    if canonical not in texts:
                        texts.append(canonical)
    else:
        texts = []
    return list(dict.fromkeys(texts))[:12]


class PublicLiteralRecall:
    version2 = False

    def __init__(self, config, spec):
        self.config, self.spec = config, spec

    def catalog(self):
        return []

    def initial_context(self):
        public = [self.config.get('public_task', '')]
        memory = self.config.get('memory')
        if memory and Path(memory).is_file():
            public.append(json.loads(Path(memory).read_text()))
        selected = literals(public)
        requested = []
        if self.spec['kind'] == 'public-literal-recall-v2':
            # These labels come from the actual public request, never an answer table.
            requested = re.findall(r'\binclude\s+(?:the\s+)?(?:current\s+)?([^\n,.]{2,100}?)\s+in\s+(?:your|the)\s+(?:final\s+)?(?:summary|report|answer)',
                                   self.config.get('public_task', ''), re.I)
            selected = list(dict.fromkeys(selected + requested))
        if not selected:
            return ''
        reminder = ('\n\nPublic representation reminders: ' + json.dumps(selected, ensure_ascii=False) +
                '\nThese strings occur in your current public task or already injected memory. '
                'Preserve exact relevant labels, delimiters, complete qualifiers and stated units '
                'when writing durable rules or the requested output. ISO forms are equivalent spellings '
                'of explicitly observed unambiguous dates, not new dates or validity decisions. '
                'Do not add fields to a fixed output layout or persist one-off task data.')
        if requested:
            reminder += (' Explicitly requested fact labels: ' + json.dumps(requested, ensure_ascii=False) +
                         '. When reporting each requested fact, preserve its name as well as the actual '
                         'value. For an unambiguous observed calendar date, include its ISO spelling '
                         'when the requested layout permits. Keep current values and historical '
                         'corrections distinct. This is representation fidelity, not a new fact or permission.')
        return reminder

    def observe(self, event):
        if event.get('error') or event['name'] not in self.spec['read_tools']:
            return None
        selected = literals(event.get('result'))
        if not selected:
            return None
        receipt = {'plugin': self.spec['kind'], 'source_actor': 'framework_plugin',
                   'source_call_id': str(event['call_id']), 'status': 'public_literals_observed',
                   'raw_sha256': digest(event['result']), 'persisted': False,
                   'information_support': [{'kind': 'public_literal_spellings', 'values': selected,
                       'boundary': 'Observed public strings only; applicability and source authority remain unverified.'}]}
        with Path(self.config['plugin_events']).open('a') as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False) + '\n')
        return receipt
