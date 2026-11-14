from copy import deepcopy
import contextlib
import hashlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import uuid

from integrations.episode_cluster import EpisodeProfile
from integrations.skill_rsi_comparison import SkillComparison
from integrations.skill_rsi_conditions import SOURCES,TABLES,compare_retained_conditions
from integrations.skill_rsi_contract import RSIError
from scripts.check_skill_rsi_conditions import main
from tests import test_skill_rsi_audit as audit_fixtures
from workenv.core import save


class ConditionsTests(unittest.TestCase):
    runner=audit_fixtures.AuditTests.runner
    service=audit_fixtures.AuditTests.service
    prepared=audit_fixtures.AuditTests.prepared

    def setUp(self):
        audit_fixtures.AuditTests.setUp(self)
        for name in SOURCES:
            target=self.root/name;target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists():target.write_text('source fixture')
        path=self.root/'configs/task_adapter_profiles.json'
        profiles=json.loads(path.read_text());profiles['profiles']['tax']={'task_id':'wc-sales-tax-summary','resource_backends':['isolated']}
        path.write_text(json.dumps(profiles))

    def retained_runner(self,root,task,settings,**kwargs):
        report=audit_fixtures.AuditTests.retained_runner(self,root,task,settings,**kwargs)
        sid=report['resources_session_id'];profile=EpisodeProfile(root,sid,0)
        base=profile.base;base.mkdir(parents=True)
        episode={'rollout_id':'fixture-'+report['run_id'],'attempt':0}
        binding={'resources_session_id':sid,'episode_id':episode,
                 'task_id':{'taskset':'toolathlon_gym:fixture','task_id':task},'task_data':{}}
        run=base/'task-session';run.mkdir()
        save(base/'cluster_owner.json',profile.record())
        save(base/'seed_identity.json',{'seed_sha256':'1'*64})
        save(base/'resources-session.json',{'schema_version':'nemo-toolathlon-resource-ledger-v1','owner':str(root),
            'sessions':{sid:{'state':'closed','binding':binding,'run_ref':run.relative_to(root).as_posix()}}})
        initial={'tables':{name:{'rows':0,'sha256':'2'*64} for name in sorted(TABLES)},'files':{}}
        save(run/'initial_state.json',initial)
        save(run/'run.json',{'task_id':task,'profile':profile.record(),'initial_state_ref':'initial_state.json',
            'initial_state_sha256':hashlib.sha256((run/'initial_state.json').read_bytes()).hexdigest()})
        report.update(episode_id=episode,prompt_identity={'commit':'3'*40,'git_blob':'4'*40,
            'path':'tasks/finalpool/'+task+'/docs/task.md','sha256':'5'*64,'checkout':'private-checkout'},
            task_profile={'profile_id':'fixture','backend':'isolated'})
        discovery={'task_profile':report['task_profile'],'tools':[{'server':'unit','name':'read','inputSchema':{'type':'object'}}]}
        directory=root/'.runtime/model-task-runs'/report['run_id']
        with (directory/'trace.jsonl').open('a') as output:
            for route,request,response in (('/seed_session',binding,{'resources_session_id':sid}),
                    ('/tools',{'resources_session_id':sid,'episode_id':episode},discovery),
                    ('/close_session',{'resources_session_id':sid,'episode_id':episode},{'resources_session_id':sid})):
                output.write(json.dumps({'kind':'nemo','value':{'route':route,'request':request,'response':response}})+'\n')
        save(directory/'manifest.json',report)
        for name in ('trace.jsonl','manifest.json'):
            report['private_input_sha256'][(directory/name).relative_to(root).as_posix()]=hashlib.sha256((directory/name).read_bytes()).hexdigest()
        save(directory/'result.json',report)
        return report

    def pair(self,task='wc-sales-tax-summary'):
        service,body=self.prepared()
        request={'schema_version':'skill-rsi-comparison-v1','request_id':str(uuid.uuid4()),'task_id':task,
                 'candidates':[None,body['skill']]}
        job=SkillComparison(service).evaluate(request)
        return request['request_id'],job['result']

    def resource(self,arm):
        report=json.loads((self.root/'.runtime/model-task-runs'/arm['result']['run_id']/'result.json').read_text())
        return self.root/'.runtime/native-episodes'/report['resources_session_id']

    def update_initial(self,base,value):
        run=base/'task-session';save(run/'initial_state.json',value)
        metadata=json.loads((run/'run.json').read_text());metadata['initial_state_sha256']=hashlib.sha256((run/'initial_state.json').read_bytes()).hexdigest()
        save(run/'run.json',metadata)

    def update_trace(self,arm,change):
        directory=self.root/'.runtime/model-task-runs'/arm['result']['run_id']
        rows=[json.loads(line) for line in (directory/'trace.jsonl').read_text().splitlines()]
        for row in rows:change(row)
        (directory/'trace.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
        report=json.loads((directory/'result.json').read_text())
        report['private_input_sha256'][(directory/'trace.jsonl').relative_to(self.root).as_posix()]=hashlib.sha256((directory/'trace.jsonl').read_bytes()).hexdigest()
        save(directory/'result.json',report)

    def test_matching_recorded_conditions_still_cannot_certify_missing_clock(self):
        identity,pair=self.pair();before=len(self.calls)
        def nemo_defaults(row):
            if row.get('kind')=='nemo' and row['value'].get('route')=='/seed_session':
                row['value']['response'].update(resources_tools=None,sandbox_access=None)
        for arm in pair['arms']:self.update_trace(arm,nemo_defaults)
        original=(self.root/'evidence/skill_rsi/comparisons'/(identity+'.json')).read_bytes()
        result=compare_retained_conditions(self.root,identity)
        self.assertTrue(result['binding_audit_passed']);self.assertTrue(result['recorded_components_equal'])
        self.assertEqual(result['dimensions']['clock_contract']['status'],'unrecorded')
        self.assertFalse(result['initial_state_equality_attested']);self.assertFalse(result['selection_eligible'])
        self.assertIsNone(result['winner']);self.assertEqual(len(self.calls),before)
        self.assertEqual(original,(self.root/'evidence/skill_rsi/comparisons'/(identity+'.json')).read_bytes())
        self.assertNotIn('private-checkout',json.dumps(result))

    def test_changed_initial_business_rows_are_reported_without_values(self):
        identity,pair=self.pair();base=self.resource(pair['arms'][1])
        initial=json.loads((base/'task-session/initial_state.json').read_text())
        initial['tables']['wc.orders']['rows']=1234567
        self.update_initial(base,initial)
        result=compare_retained_conditions(self.root,identity)
        self.assertEqual(result['dimensions']['recorded_initial_data']['status'],'different')
        self.assertFalse(result['recorded_components_equal']);self.assertNotIn('1234567',json.dumps(result))

    def test_raw_tool_schema_difference_is_not_hidden_by_skill_permissions(self):
        identity,pair=self.pair()
        def change(row):
            if row.get('kind')=='nemo' and row['value'].get('route')=='/tools':
                row['value']['response']['tools'][0]['inputSchema']['required']=['PRIVATE_TOOL_FIELD']
        self.update_trace(pair['arms'][1],change)
        result=compare_retained_conditions(self.root,identity)
        self.assertEqual(result['dimensions']['original_tools']['status'],'different')
        self.assertNotIn('PRIVATE_TOOL_FIELD',json.dumps(result));self.assertFalse(result['selection_eligible'])

    def test_seed_identity_difference_is_visible_and_missing_evidence_is_not_inferred(self):
        identity,pair=self.pair();base=self.resource(pair['arms'][1])
        save(base/'seed_identity.json',{'seed_sha256':'6'*64})
        self.assertEqual(compare_retained_conditions(self.root,identity)['dimensions']['sql_seed_record']['status'],'different')
        (base/'seed_identity.json').unlink()
        with self.assertRaisesRegex(RSIError,'conditions_input_unavailable'):compare_retained_conditions(self.root,identity)

    def test_partial_task_snapshot_is_not_promoted_to_complete_initial_data(self):
        identity,pair=self.pair('unit');result=compare_retained_conditions(self.root,identity)
        self.assertEqual(result['dimensions']['recorded_initial_data']['status'],'unrecorded')
        self.assertFalse(result['recorded_components_equal']);self.assertFalse(result['initial_state_equality_attested'])

    def test_initial_hash_damage_is_rejected(self):
        identity,pair=self.pair();base=self.resource(pair['arms'][0])
        save(base/'task-session/initial_state.json',{'PRIVATE_CORRUPTION':True})
        with self.assertRaisesRegex(RSIError,'conditions_initial_hash_mismatch'):compare_retained_conditions(self.root,identity)

    def test_resource_episode_and_reference_must_match(self):
        identity,pair=self.pair();base=self.resource(pair['arms'][0]);path=base/'resources-session.json'
        original=json.loads(path.read_text());sid=next(iter(original['sessions']))
        for changed in ('episode','reference'):
            value=deepcopy(original)
            if changed=='episode':value['sessions'][sid]['binding']['episode_id']={'rollout_id':'different','attempt':0}
            else:value['sessions'][sid]['run_ref']='../PRIVATE_OUTSIDE'
            save(path,value)
            with self.assertRaises(RSIError):compare_retained_conditions(self.root,identity)

    def test_duplicate_or_rebound_discovery_is_rejected(self):
        identity,pair=self.pair()
        def change(row):
            if row.get('kind')=='nemo' and row['value'].get('route')=='/tools':
                row['value']['request']['resources_session_id']=str(uuid.uuid4())
        self.update_trace(pair['arms'][0],change)
        with self.assertRaisesRegex(RSIError,'conditions_trace_binding_invalid'):compare_retained_conditions(self.root,identity)

    def test_comparison_must_pass_original_intent_binding_first(self):
        identity,pair=self.pair();path=self.root/'evidence/skill_rsi/comparisons'/(identity+'.json')
        pair['arms'].reverse();save(path,pair)
        with self.assertRaisesRegex(RSIError,'conditions_comparison_audit_failed'):compare_retained_conditions(self.root,identity)

    def test_cli_exposes_fixed_metadata_and_never_echoes_invalid_identity(self):
        identity,pair=self.pair();output=io.StringIO()
        with patch('scripts.check_skill_rsi_conditions.ROOT',self.root),contextlib.redirect_stdout(output):
            status=main(['--comparison-id',identity])
        self.assertEqual(status,0);self.assertFalse(json.loads(output.getvalue())['selection_eligible'])
        output=io.StringIO()
        with contextlib.redirect_stdout(output):status=main(['--comparison-id','PRIVATE_INVALID_ID'])
        self.assertEqual(status,2);self.assertNotIn('PRIVATE_INVALID_ID',output.getvalue())

if __name__=='__main__':unittest.main()
