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
        terms=r'\b(current|updated|approval|approved|permission|identity|exception|alias|date|standing|scope|changed)\b'
        if parent.variant2:
            terms=r'\b(current|updated|approval|approved|permission|identity|exception|alias|date|standing|scope|changed|today|now|recent|latest)\b'
        if re.search(terms, task, re.I):
            return 3, 'public_task_has_contextual_dependency'
        return 0, ''

    def guidance_for(self, parent):
        if not parent.variant2:
            return self.guidance
        text = (self.guidance+' A record creation/update timestamp is metadata, not by itself an '
                'eligibility restriction or the task business clock. Do not refuse an otherwise matched '
                'requested operation merely because that timestamp differs from the host date. '
                'Respect explicit date filters and expiry conditions when they actually apply; preserve '
                'the stated source dates and finish independent requested operations.')
        if parent.variant4:
            text+=(' For a real partially supported write, information_assess accepts field_requirements '
                   'by original argument name. Its compiled ready_actions omit only unsupported optional '
                   'fields. Execute those exact partial arguments with the original tool, then report '
                   'remaining fields explicitly; required target/authorization dependencies still apply.')
        return text

    def observe(self, parent, event):
        facts = [{'pointer': p, 'value': v, 'source_call_id': str(event['call_id'])}
                 for p, v in leaves(event['result'])
                 if isinstance(v, (str, int, float)) and not isinstance(v, bool)
                 and re.search(r'(date|valid|expir|alias|email|authoriz|version|effective)', p, re.I)]
        if parent.variant2:
            facts.extend({'pointer':p,'value':v,'source_call_id':str(event['call_id']),
                          'field_role':'record_metadata_time_not_by_itself_task_clock_or_eligibility'}
                         for p,v in leaves(event['result']) if isinstance(v,str)
                         and p.rsplit('/',1)[-1] in ('created_at','updated_at'))
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

    def guidance_for(self, parent):
        if not parent.variant3:
            return self.guidance
        text = ('Use complete public procedures when available and respect any explicit requirement to '
                'use a particular procedure. When the task instead delegates ordinary triage or appropriate '
                'judgment, use the actual case descriptions and available guidelines to make that judgment; '
                'do not add a new requirement for an authoritative rulebook or exact prescribed labels. '
                'Separate such delegated choices from genuinely unavailable fixed codes or protocol meanings. '
                'If only some fields lack support, omit or mark those fields unresolved and complete the '
                'other requested supported updates. Do not use a general missing-procedure claim as a '
                'context prerequisite that blocks all work, and do not invent hidden rules.')
        if parent.variant4:
            text+=(' Distinguish the uncertainty of a changed threshold or special tag from all other '
                   'observed facts. A partially outdated rule is neither automatically current nor '
                   'grounds to discard every independently supported action. Register genuine mapping '
                   'gaps per argument using field_requirements; never manufacture a missing new rule '
                   'or send a selector-only update to look complete.')
        if parent.variant5:
            text+=(' Read conditional instructions literally: if a source is required only when '
                   'available, first check its actual availability; do not turn that condition into '
                   'a requirement that the source must exist. An unconditional requirement for a '
                   'specific procedure or authorization still applies. A missing detailed mapping '
                   'does not erase the public task or case facts as independent evidence for '
                   'ordinary delegated choices. Assess each requested field separately: which '
                   'actually needs a fixed external rule, and which is supported by the task and '
                   'observations? Do not invent exact unavailable codes or call a judgment the SOP '
                   'rule. For missing optional fields, field_requirements may name an original '
                   'schema property absent from arguments; no placeholder or guessed value is needed. '
                   'Cite successful source IDs and paths, using the latest read of a repeated source.')
        return text

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
