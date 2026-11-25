"""Independent source-body recovery without an action-plan requirement."""
from .bridge import EvidenceBridge, failed


class SourceBodyRecovery(EvidenceBridge):
    def __init__(self, config, spec):
        super().__init__(config, {**spec, 'kind': 'evidence-bridge-v9'})
        self.spec = spec

    def initial_context(self):
        return ''

    def catalog(self):
        return []

    def check_before(self, name, args, call_id):
        pass

    def commit_before(self, name, args, call_id):
        pass

    def observe(self, event):
        if not self.is_read(event['name'], event.get('arguments', {})):
            return None
        if event.get('error') or failed(event.get('result')):
            return None
        from .recovery import expand, follow_document_references
        additions = expand(self, event)
        if self.spec.get('follow_document_references'):
            additions += follow_document_references(self, event)
        if not additions:
            return None
        return self.record('source_bodies_returned', source_call_id=str(event['call_id']),
                           source_expansions=additions, persisted=False)
