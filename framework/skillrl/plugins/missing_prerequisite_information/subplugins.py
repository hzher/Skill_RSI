"""Three public-evidence strategies; no benchmark labels or answer tables."""
import re
from .bridge import leaves, failed
from ..common import digest


class SourceAccess:
    name = 'source_access'
    guidance = ('Use only the currently available history/Skill channels. When a prior decision is '
                'unavailable, check permitted current sources; report the exact remaining gap and '
                'continue independent work. An empty search does not establish global absence.')

    def probe(self, parent, event=None):
        task = parent.config.get('public_task', '')
        if event and event['name'] == 'session_search':
            return 4, 'observed_history_read'
        if re.search(r'\b(prior|previous|saved|standing|established|persisted|history|historical|skill)\b', task, re.I):
            return 2, 'public_task_requests_prior_context'
        return 0, ''

    def observe(self, parent, event):
        from .recovery import follow_history
        expanded = follow_history(parent, event) if parent.spec.get('follow_history_references') else []
        return expanded, []


class ContextRequirements:
    name = 'context_requirements'
    guidance = ('Separate known facts from unresolved applicability. Preserve and report known '
                'historical decisions and their exact source even when present validity is uncertain. '
                'Use the business reference time supplied by the task or sources; do not substitute '
                'the host clock. Do not guess a new destination, identity, approval or unseen preference. '
                'Use information_assess only for a real unresolved dependency; ordinary supported work '
                'does not require a plan or additional proof.')

    def probe(self, parent, event=None):
        task = parent.config.get('public_task', '')
        if re.search(r'\b(current|updated|approval|approved|permission|identity|exception|alias|date|standing|scope|changed)\b', task, re.I):
            return 3, 'public_task_has_contextual_dependency'
        return 0, ''

    def observe(self, parent, event):
        facts = [{'pointer': p, 'value': v, 'source_call_id': str(event['call_id'])}
                 for p, v in leaves(event['result'])
                 if isinstance(v, (str, int, float)) and not isinstance(v, bool)
                 and re.search(r'(date|valid|expir|alias|email|authoriz|version|effective)', p, re.I)]
        if not facts:
            return [], []
        return [], [{'kind': 'observed_context_fields', 'facts': facts[:8],
                     'semantic_applicability': 'unverified',
                     'source_sha256': digest(event['result'])}]


class ProcedureCompletion:
    name = 'procedure_completion'
    guidance = ('Follow public source_expansions to the actual complete procedure. A brief, operation '
                'name or single example may leave mappings unresolved. Apply the supported parts of '
                'the public instructions and examples, including independent work; do not turn one '
                'missing mapping into a blanket refusal. Read useful saved Skill bodies when allowed. '
                'Do not invent exact tags, parameter meanings or unavailable rules.')

    def probe(self, parent, event=None):
        if event and (any(r['source_tool'] == event['name'] for r in parent.spec.get('source_expansions', []))
                      or event['name'] in ('kb_get_article', 'Skill')):
            return 4, 'observed_procedure_source'
        if re.search(r'\b(procedure|policy|protocol|sop|guidelines?|rules?|standards?|triage|classif\w*|incident|correlation|engagement|rollback)\b',
                     parent.config.get('public_task', ''), re.I):
            return 2, 'public_task_requests_procedure'
        return 0, ''

    def observe(self, parent, event):
        from .recovery import expand, follow_document_references
        expanded = expand(parent, event)
        if parent.spec.get('follow_document_references'):
            expanded += follow_document_references(parent, event)
        return expanded, []


CHILDREN = {cls.name: cls() for cls in (SourceAccess, ContextRequirements, ProcedureCompletion)}
