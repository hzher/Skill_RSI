"""Opt-in public-evidence procedures; never consume evaluation contracts."""

EXECUTION_RELIABILITY = '''
Execution procedure:
1. Before making changes, identify the requested deliverable, authorized targets, exact fields,
   excluded targets, and prohibited actions from the public request. Keep a short working checklist.
2. If the request refers to a saved procedure or prior decision, read the applicable complete skill
   and retrieve the authorized history before acting. A catalog entry is not the procedure itself.
   For historical lookup, search the named entity and decision type, inspect all plausible matches,
   and compare scope, environment, approval status, exceptions and validity. Do not select a record
   merely because its ID is larger, its wording is similar, or it is newer. A quoted draft or an
   unrelated environment is not an authoritative replacement. If a search is empty or ambiguous,
   try a narrower entity/reference query and inspect the original messages before concluding absence.
3. Turn the applicable rule into an exact action plan: which records match every condition, which
   records are exceptions, and which field values the source actually authorizes. Preserve unrelated
   fields. Apply recipient restrictions to the requested recipients, not all meeting attendees.
4. Perform each authorized action with its tool. Identifying a target, describing an update, or
   intending to share is not execution. When a call fails, inspect its actual error and correct only
   the supported issue; do not blindly repeat a possibly completed write.
5. Before finishing, reconcile the checklist against successful tool results. Where a read tool is
   available, verify changed fields and status. Check for missing required actions and unintended
   recipients/records; repair supported omissions while the environment is still available.
6. Follow the requested final format and retain exact source identifiers, dates, field values and
   scoped exceptions. Report completed actions and unresolved limitations accurately. Do not invent
   missing policy, request redundant confirmation for an already authorized action, or save one-off
   values as global preferences. If information is genuinely unavailable, state the specific gap.
Use only the public task and authorized tool evidence. Private expectations, evaluator answers and
future tasks are unavailable. This procedure does not override task permissions or disabled tools.
'''

CURATION_RELIABILITY = '''
Preserve an executable procedure rather than only a topic summary. Read complete relevant evidence
and parent skills before changing them. When public instructions, authoritative source documents,
or explicit tool validation feedback establish a rule, retain its applicability conditions, exact
field names and supported values, grouping keys, negative cases, exceptions, and completion checks.
Keep source-backed action schemas when generalizing away instance IDs. Do not drop a correct clause
just because a later task supplies only a brief overview or does not repeat the rule. A missing
document in a later task is not evidence that an inherited authoritative clause became obsolete.
Conversely, successful tool acceptance or an assistant's unsupported guess does not establish policy.
Separate confirmed rules from uncertainty; do not promote guessed labels or task-specific answers
into durable rules. If evidence lacks an action schema, retain the known rule and the precise gap.
When a source contains an exception registry or historical-decision lookup method, preserve how to
locate the applicable entry and distinguish scope/version, rather than hard-coding the current case.
Include verification of actual successful writes, exact target sets and unchanged excluded records.
No-op remains valid; no need to create a skill for every task or harmless formatting variation.
'''


EPISODIC_EXECUTION = '''
Historical evidence procedure:
- Use only tools enabled in this session. A prior decision belongs in authorized session history,
  not in a new global memory rule. Do not try to enable missing persistence capabilities.
- Distinguish a session identifier from a business record identifier. Once a search returns a
  relevant session, inspect the original successful tool-return content inside that session;
  use exact session lookup if necessary. A record absent from today's reset service does not
  invalidate its original successful historical read. Do not replace positive historical evidence
  with a later not-found response from a different service snapshot.
- Read response bodies: HTTP success alone does not establish business success. Treat an error
  payload, not-found result or empty match as such and preserve its scope when reporting it.
- For an absence claim, cover both authorized history and the current relevant public records
  when those sources are available. Do not search unrelated history for a self-contained task.
- Preserve source identifiers, the original decision, restrictions, exception scope and expiry
  text. Separate an explicit date from an inferred year, and an original authorization from
  present validity. Use a reference clock only when the public environment supplies one; do not
  silently assume that a source creation year is an expiry year or that an old exception is live.
- Before emitting a compact answer, check that compression has not removed the actual exception,
  its limiting conditions, the requested time information or the source citation. Place needed
  detail inside the requested fields. Cite retrieved evidence rather than an earlier paraphrase.
'''

EPISODIC_CURATION = '''
For episodic lookup, retain a reusable evidence-handling method, not a case answer. Keep original
historical tool-return facts distinct from later assistant interpretation and current service
availability. Preserve how to locate and cite the source, how to handle date uncertainty and scope,
and how to verify business success from the response body. Do not replace source facts with an
unsupported year, current-validity assumption or a later not-found result from a reset service.
'''

SCOPE_PRECEDENCE = '''
Resolve the public task's scope BEFORE applying any reusable procedure:
- Separate affirmative work requests from protected or excluded subjects. Mentioning a previous
  decision only to say "do not change/revisit/search it" is a boundary, not a recall request.
- Explicit read/search restrictions in the current task take precedence over general evidence
  gathering advice below. Do not retrieve excluded history to prove it was left unchanged.
- For a current-records-only audit, use current scoped records and report only those findings.
  Do not append unrelated historical approvals, even if they are already known or available.
- Retrieve history only when historical evidence is affirmatively needed for the authorized
  deliverable and the task permits that retrieval. Preserve excluded decisions by leaving them
  alone, rather than reconstructing them or revalidating them without a request.
'''


def reliability_guidance(policy, *, curator=False):
    if policy not in ('legacy', 'verified-v1', 'verified-bounded-v1', 'episodic-evidence-v1', 'scoped-episodic-v2'):
        raise ValueError('Unknown execution reliability policy: ' + str(policy))
    if policy == 'legacy':
        return ''
    base = CURATION_RELIABILITY if curator else EXECUTION_RELIABILITY
    if policy == 'scoped-episodic-v2':
        if curator:
            return base + EPISODIC_CURATION + '\nPreserve public read/search exclusions as procedure applicability boundaries, not as requests to recover excluded case facts.\n'
        base = base.replace('2. If the request refers to a saved procedure or prior decision, read the applicable complete skill\n   and retrieve the authorized history before acting.',
                            '2. Only when the public request affirmatively requires a permitted saved procedure or historical\n   decision, read the applicable complete skill and retrieve authorized history before acting.')
        return SCOPE_PRECEDENCE + base + EPISODIC_EXECUTION
    if policy == 'episodic-evidence-v1':
        return base + (EPISODIC_CURATION if curator else EPISODIC_EXECUTION)
    return base
