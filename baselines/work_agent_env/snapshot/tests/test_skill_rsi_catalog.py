import hashlib
import json
import unittest
import uuid

from integrations.skill_rsi_catalog import tool_catalog
from integrations.skill_rsi_contract import RSIError
from tests import test_skill_rsi as fixtures


class CatalogTests(unittest.TestCase):
    setUp = fixtures.SkillRSITests.setUp
    runner = fixtures.SkillRSITests.runner
    service = fixtures.SkillRSITests.service

    def retained(self):
        directory=self.root/'.runtime/model-task-runs'/uuid.uuid4().hex;directory.mkdir(parents=True)
        identity={'run_id':directory.name,'task_id':'unit','environment_source_sha256':{'fixture':'environment'}}
        manifest=directory/'manifest.json';manifest.write_text(json.dumps(identity))
        bound={'resources_session_id':str(uuid.uuid4()),'episode_id':{'rollout_id':'fixture','attempt':0}}
        trace=directory/'trace.jsonl';trace.write_text(json.dumps({'kind':'nemo','value':{'route':'/tools','request':bound,'response':{
            'tools':[{'server':'unit','name':'read','inputSchema':{'type':'object'},'description':'Read data'}]}}})+'\n'
            +json.dumps({'kind':'nemo','value':{'route':'/close_session','request':bound,
                'response':{'resources_session_id':bound['resources_session_id']}}})+'\n')
        (directory/'result.json').write_text(json.dumps({**identity,**bound,'closed':True,'source_unchanged':True,
            'private_input_sha256':{p.relative_to(self.root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (manifest,trace)}}))
        return directory

    def test_catalog_is_environment_bound_and_contains_only_contracts(self):
        service=self.service();self.retained();result=tool_catalog(service,'unit')
        self.assertIn('unit__read',{t['name'] for t in result['tools']})
        self.assertFalse(result['live_availability_attested']);self.assertEqual(self.calls,[])
        self.assertNotIn('PRIVATE business rows',json.dumps(result))

    def test_absent_or_tampered_discovery_is_not_guessed(self):
        service=self.service()
        with self.assertRaisesRegex(RSIError,'unavailable'):tool_catalog(service,'unit')
        directory=self.retained();(directory/'trace.jsonl').write_text('tampered')
        with self.assertRaisesRegex(RSIError,'hash_mismatch'):tool_catalog(service,'unit')

    def test_manifest_mutation_is_rejected_even_when_trace_hash_is_valid(self):
        service=self.service();directory=self.retained();path=directory/'manifest.json'
        manifest=json.loads(path.read_text());manifest['started_at']='later'
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RSIError,'catalog_discovery_binding_mismatch'):tool_catalog(service,'unit')

    def test_result_task_environment_and_run_must_match_discovery_manifest(self):
        service=self.service();directory=self.retained();path=directory/'result.json';original=json.loads(path.read_text())
        for key,value in (('task_id','different'),('environment_source_sha256',{'other':'environment'}),('run_id','0'*32)):
            path.write_text(json.dumps({**original,key:value}))
            with self.subTest(key=key),self.assertRaisesRegex(RSIError,'catalog_discovery_binding_mismatch'):
                tool_catalog(service,'unit')

    def test_tools_and_close_are_bound_to_the_original_resource_and_episode(self):
        service=self.service();directory=self.retained();path=directory/'trace.jsonl'
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        report_path=directory/'result.json';report=json.loads(report_path.read_text())
        cases=[rows[:1],list(reversed(rows)),json.loads(json.dumps(rows)),json.loads(json.dumps(rows))]
        cases[2][0]['value']['request']['resources_session_id']=str(uuid.uuid4())
        cases[3][1]['value']['response']['resources_session_id']=str(uuid.uuid4())
        for index,changed in enumerate(cases):
            path.write_text(''.join(json.dumps(row)+'\n' for row in changed))
            report['private_input_sha256'][path.relative_to(self.root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
            report_path.write_text(json.dumps(report))
            with self.subTest(case=index),self.assertRaisesRegex(RSIError,'catalog_discovery_binding_mismatch'):
                tool_catalog(service,'unit')

    def test_duplicate_discovery_or_mismatched_profile_cannot_supply_a_catalog(self):
        service=self.service();directory=self.retained();path=directory/'trace.jsonl'
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        report_path=directory/'result.json';original=json.loads(report_path.read_text())
        for changed,report in (([rows[0],*rows],original),
            (rows,{**original,'task_profile':{'task_id':'unrelated'}})):
            path.write_text(''.join(json.dumps(row)+'\n' for row in changed))
            report['private_input_sha256'][path.relative_to(self.root).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
            report_path.write_text(json.dumps(report))
            with self.assertRaisesRegex(RSIError,'catalog_discovery_binding_mismatch'):tool_catalog(service,'unit')


if __name__=='__main__':unittest.main()
