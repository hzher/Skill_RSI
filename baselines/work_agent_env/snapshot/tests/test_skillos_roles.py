"""Role isolation, real protocol parsing, failure retention and reward binding."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from integrations.benchmark_tasks.contracts import digest
from integrations.skill_generator.roles import SkillOSRoles, load_roles, final_text, validate_reward
from integrations.skill_generator.transport import normalize_response
from integrations.skill_generator.editor import SkillEditor
from integrations.skill_library import SkillLibrary


class Tokenizer:
    def apply_chat_template(self, messages, **kwargs):
        return [10, 11, 12]

    def encode(self, text, **kwargs):
        return [1] * len(text.split())


class RoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.config = load_roles()
        self.roles = SkillOSRoles(self.config, tokenizers={key: Tokenizer() for key in self.config["roles"]})
        self.addAsyncCleanup(self.roles.close)

    async def transport(self, handler):
        await self.roles.client.aclose()
        def wrapped(request):
            response=handler(request)
            value=response.json()
            if not any('prompt_token_ids' in row for row in value.get('choices',[])):
                value.setdefault('prompt_token_ids',json.loads(request.content)['prompt'])
            return httpx.Response(response.status_code,json=value)
        self.roles.client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))

    async def test_judge_budget_escalation_preserves_real_attempts(self):
        seen = []
        def handler(request):
            body = json.loads(request.content)
            seen.append(body)
            text = 'not JSON' if len(seen) == 1 else '<think>diagnostic reasoning</think>{"success":false,"rationale":"missing evidence","evidence_step":-1}'
            return httpx.Response(200, json={"choices": [{"text": text, "finish_reason": "stop"}]})
        await self.transport(handler)
        view = {"task_description": "create report", "steps": [{"step": 1, "action": "final_response", "observation": "I did it"}]}
        result = await self.roles.selfjudge(view, directory=self.directory)
        self.assertFalse(result["parsed"]["success"])
        self.assertEqual([row["max_tokens"] for row in seen], [4096, 8192])
        self.assertEqual(len(list(self.directory.glob('*.sent.json'))), 2)
        self.assertEqual(result['attempts'][0]['budget'],4096)

    async def test_unknown_transport_is_not_replayed(self):
        calls = []
        def handler(request):
            calls.append(1)
            raise httpx.ReadTimeout('diagnostic fixture')
        await self.transport(handler)
        with self.assertRaises(httpx.ReadTimeout):
            await self.roles.query('contentjudge',[{'role':'user','content':'test'}],directory=self.directory)
        self.assertEqual(len(calls),1)

    async def test_final_claim_is_not_selfjudge_evidence(self):
        await self.transport(lambda request: httpx.Response(200,json={'choices':[{
            'text':'{"success":true,"rationale":"agent claimed success","evidence_step":1}', 'finish_reason':'stop'}]}))
        with self.assertRaisesRegex(ValueError, 'exhausted'):
            await self.roles.selfjudge({'task_description':'task','steps':[{
                'step':1,'action':'final_response','observation':'done'}]},directory=self.directory)

    async def test_invalid_skill_action_retained_without_changing_parent(self):
        library = SkillLibrary(self.directory/'library')
        editor = SkillEditor(None,library)
        body={'parent':library.empty_version,'experience_id':'fixture','task_uid':'fixture:seed',
              'generator_policy_id':'initial'}
        request={'id':digest(body),**body}
        candidate=editor.apply(request,'<think>unfinished format</think>{bad}',record_failures=True)
        self.assertEqual(candidate['version'],library.empty_version)
        self.assertFalse(candidate['operations'][0]['valid'])
        self.assertEqual(candidate['provenance']['response'],'<think>unfinished format</think>{bad}')
        with self.assertRaises(ValueError):editor.apply(request,'{bad}')

    async def test_reward_contains_judges_and_original_evaluator_separately(self):
        def handler(request):
            body=json.loads(request.content)
            text='{"success":false,"rationale":"missing proof","evidence_step":-1}' if body['model']=='skillos-selfjudge-frozen' else '{"VALID":true,"ISSUES":[],"EXPLANATION":"reusable"}'
            return httpx.Response(200,json={'choices':[{'text':text,'finish_reason':'stop'}]})
        await self.transport(handler)
        candidate={'id':'fixture','skills':{'Example':'---\nname: Example\ndescription: test\n---\n# Workflow\nCheck requirements.'},
                   'operations':[{'valid':True,'executed':True,'judge_content':'Example skill'}]}
        view={'task_uid':'fixture:downstream','experience_id':'fixture-experience','task_description':'task',
              'steps':[{'step':1,'action':'inspect_output','observation':'missing proof'}],
              'result':{'status':'measured','reward':1,'passed':True}}
        result=await self.roles.assess(candidate=candidate,downstream_view=view,prompt_ids=[1]*100,directory=self.directory)
        self.assertEqual(result['components']['task'],0)
        self.assertEqual(result['components']['content'],1)
        self.assertEqual(result['original_evaluation']['reward'],1)
        row={'candidate_id':candidate['id'],'reward':result['reward'],'attribution':{
            'task_uid':view['task_uid'],'downstream_experience_id':view['experience_id'],'reward':1}}
        validate_reward(result,row=row,config=self.config)
        with self.assertRaises(ValueError):validate_reward(result,row=row,config=None)
        altered=deepcopy(result);altered['components']['task']=1
        with self.assertRaises(ValueError):validate_reward(altered,row=row,config=self.config)

    async def test_reasoning_separation_preserves_raw_identity(self):
        raw='<think>diagnostic thought</think>\n[]'
        normalized,metadata=normalize_response(raw)
        self.assertEqual(json.loads(normalized),[])
        self.assertTrue(metadata['reasoning_channel_removed'])
        self.assertEqual(metadata['raw_sha256'],digest(raw))

    async def test_production_model_and_thinking_config(self):
        from training.markdown_launch import load_config,trainer_config
        root=Path(__file__).resolve().parents[1]
        config=load_config(root/'configs/generator_h20_smoke.json')
        native=trainer_config(config,self.directory)
        self.assertTrue(config['model_path'].endswith('/Qwen3-8B'))
        self.assertTrue(native['data']['apply_chat_template_kwargs']['enable_thinking'])
        self.assertEqual(native['actor_rollout_ref']['rollout']['temperature'],1)
        self.assertTrue(self.config['roles']['selfjudge']['frozen'])
        self.assertNotEqual(self.config['roles']['curator']['base_url'],self.config['roles']['selfjudge']['base_url'])

    async def test_stream_builder_excludes_same_prompt_and_reserved_tasks(self):
        from training.task_stream import propose_edges
        def task(identity,prompt):
            return {'uid':'fixture:'+identity,'task_id':identity,'prompt':prompt,
                    'tools':[{'kind':'mcp_server','name':'excel'}]}
        source=task('seed','Review a workbook and email the result.')
        target=task('next','Review a different workbook and summarize it.')
        duplicate=task('alias',source['prompt'])
        reserved=task('reserved','Review a workbook.')
        record={'task':source}
        output=propose_edges([('experience',record,{'result':{'status':'measured'}})],
            [source,target,duplicate,reserved],evaluation_manifest={'task_ids':['reserved'],'family_roots':['reserved']},parent='empty')
        self.assertEqual(len(output['proposals']),1)
        proposal=output['proposals'][0]
        self.assertEqual(proposal['pair']['downstream_task']['uid'],target['uid'])
        self.assertFalse(proposal['training_admitted'])
        self.assertIsNone(proposal['pair']['seed_provenance'])
        self.assertEqual(output['reviewed_pairs_written'],0)

    async def test_partial_checkpoint_directory_cannot_pass_preflight(self):
        from training.markdown_launch import load_config,preflight
        root=Path(__file__).resolve().parents[1]
        config=load_config(root/'configs/markdown_training.example.json')
        model=self.directory/'model';model.mkdir()
        (model/'.download_chunks_model-00001-of-00005.safetensors').mkdir()
        config['model_path']=str(model)
        check=next(row for row in preflight(config)['checks'] if row['name']=='trainable_checkpoint')
        self.assertFalse(check['passed'])

    async def test_server_prompt_token_drift_is_rejected_before_parsing(self):
        await self.transport(lambda request:httpx.Response(200,json={'prompt_token_ids':[999],
            'choices':[{'text':'[]','finish_reason':'stop'}]}))
        with self.assertRaisesRegex(ValueError,'prompt token IDs'):
            await self.roles.query('curator',[{'role':'user','content':'test'}],directory=self.directory)

    async def test_only_explicit_native_budget_termination_is_retained(self):
        from training.markdown_contract import validate_sample
        policy={'run_id':'fixture','step':0,'policy_id':'initial','model_revision':'fixture-model','tokenizer_revision':'fixture-tokenizer'}
        sample={'schema_version':'workagent-generator-sample-v1','kind':'verl_native','actor':'skill_generator',
            'request_id':'request','provider_request_id':'fixture-request','policy':policy,'prompt_ids':[1],
            'response_ids':[2],'response_logprobs':[-1.0],'text':'unfinished','finish_reason':'length'}
        with self.assertRaises(ValueError):validate_sample(sample,policy=policy,request_id='request')
        sample['terminal_contract']='skillos_budget_failure_retained'
        validate_sample(sample,policy=policy,request_id='request')
        sample['kind']='diagnostic'
        with self.assertRaises(ValueError):validate_sample(sample,policy=policy,request_id='request')

    async def test_vllm_choice_level_prompt_ids_are_verified(self):
        await self.transport(lambda request:httpx.Response(200,json={'choices':[{
            'text':'[]','finish_reason':'stop','prompt_token_ids':json.loads(request.content)['prompt']}]}))
        reply=await self.roles.query('curator',[{'role':'user','content':'test'}],directory=self.directory)
        self.assertEqual(reply['text'],'[]')
        self.assertEqual(reply['prompt_ids_location'],'choice')

    async def test_unknown_or_empty_evidence_is_quarantined_without_model_calls(self):
        calls=[]
        await self.transport(lambda request:calls.append(request))
        for view in [{'task_description':'task','steps':[]},
                     {'task_description':'task','steps':[{'step':1,'action':'tool','observation':'unknown'}],'result':{'status':'unknown'}}]:
            with self.assertRaisesRegex(ValueError,'quarantined'):
                await self.roles.selfjudge(view,directory=self.directory)
        self.assertEqual(calls,[])

    async def test_reserved_prompt_metadata_supports_task_pool_root_and_repo_root_paths(self):
        from scripts.propose_task_stream import reserved_prompt_objects
        index={'root_git_tree':'tree-pin','task_ids':['reserved']}
        for path in ['reserved/docs/task.md','tasks/finalpool/reserved/docs/task.md']:
            tree={'sha':'tree-pin','truncated':False,'tree':[{'type':'blob','path':path,'sha':'prompt-pin'},
                {'type':'blob','path':'unknown/docs/task.md','sha':'not-reserved'}]}
            self.assertEqual(reserved_prompt_objects(tree,index),{'prompt-pin'})
            tree['sha']='different-pin'
            with self.assertRaises(ValueError):reserved_prompt_objects(tree,index)

    async def test_executor_access_rejection_stops_unstarted_tasks_in_all_streams(self):
        from integrations.orchestration.skillos_stream import run_stream
        from integrations.benchmark_tasks import make_task_set,load_task_set
        from integrations.benchmark_tasks.store import TaskStore
        from tests.test_generic_runtime import install
        root=self.directory/'rejected-project';root.mkdir()
        tasks=[]
        for index in range(6):
            name='denied_'+str(index);install(root,name)
            tasks.extend(load_task_set(TaskStore(root).register(name)['task_set'])['tasks'])
        attempted=[]
        class Backend:
            async def execute_async(self,selected,options):
                task=selected['tasks'][0];attempted.append(task['uid'])
                await asyncio.sleep(.02)
                return {'results':[{'task_uid':task['uid'],'status':'execution_unknown','resources_closed':True,
                    'error_details':{'status_code':403,'code':'GROUP_DELETED'}}]}
        self.roles.preflight=AsyncMock(return_value={'ready':True,'roles':[]})
        args=SimpleNamespace(streams=2,roles_config=Path(__file__).resolve().parents[1]/'configs/skillos_roles.json',
            provider='openai',model='fixed-api',max_turns=3,max_output_tokens=256,task_timeout=10,
            request_timeout=10,run_id='denied_fixture',seed=42)
        output=root/'trial';output.mkdir();receipt={'results':[],'rewarded_candidates':0}
        with patch('integrations.orchestration.skillos_stream.APIExecutor',lambda **kwargs:Backend()), \
             patch('integrations.orchestration.skillos_stream.SkillOSRoles',lambda config:self.roles):
            code=await run_stream(args,make_task_set(tasks),output,receipt,root=root)
        self.assertEqual(code,2)
        self.assertEqual(receipt['status'],'blocked_executor_access')
        self.assertEqual(len(attempted),2)
        self.assertEqual(receipt['actual_task_attempts'],2)
        self.assertEqual(len(receipt['not_started']),4)
        self.assertTrue(receipt['resources_closed'])

    async def test_independent_streams_execute_their_own_skills_and_reward_future_tasks(self):
        from integrations.orchestration.skillos_stream import run_stream
        from integrations.executors.api import APIExecutor
        from integrations.benchmark_tasks import make_task_set,load_task_set
        from integrations.benchmark_tasks.store import TaskStore
        from tests.test_generic_runtime import install,Driver
        root=self.directory/'project';root.mkdir()
        tasks=[]
        for index in range(4):
            name='fixture_'+str(index);install(root,name)
            tasks.extend(load_task_set(TaskStore(root).register(name)['task_set'])['tasks'])
        active=peak=counter=0
        class Environment:
            def __init__(self,**kwargs):self.value=None
            async def initialize(self):
                nonlocal active,peak
                active+=1;peak=max(peak,active)
                await asyncio.sleep(.02)
                return {'image_id':'fixture-image'}
            def discover(self):return [{'name':'write_file','description':'Write','parameters':{'type':'object'}}]
            async def call(self,name,arguments):self.value=arguments['content'];return {'ok':True}
            async def evaluate(self):return {'pass':self.value=='hello\n'}
            async def close(self):
                nonlocal active
                active-=1
                return {'resources_closed':True}
        def handler(request):
            nonlocal counter
            body=json.loads(request.content)
            if body['model']=='skillos-curator':
                counter+=1;name='Skill '+str(counter)
                text=json.dumps([{'name':'new_skill_insert','arguments':{'skill_name':name,
                    'content':'---\nname: '+name+'\ndescription: test\n---\n# Workflow\nCheck the required output.'}}])
            elif body['model']=='skillos-selfjudge-frozen':
                text='{"success":false,"rationale":"fixture missing proof","evidence_step":-1}'
            else:text='{"VALID":true,"ISSUES":[],"EXPLANATION":"fixture"}'
            return httpx.Response(200,json={'choices':[{'text':text,'finish_reason':'stop'}]})
        await self.transport(handler)
        self.roles.preflight=AsyncMock(return_value={'ready':True,'roles':[]})
        args=SimpleNamespace(streams=2,roles_config=Path(__file__).resolve().parents[1]/'configs/skillos_roles.json',
            provider='openai',model='test-driver',max_turns=3,max_output_tokens=256,task_timeout=10,
            request_timeout=10,run_id='stream_fixture',seed=42)
        output=root/'trial';output.mkdir()
        receipt={'results':[],'rewarded_candidates':0}
        def executor(**kwargs):return APIExecutor(**kwargs,model_factory=Driver,environment_factory=Environment)
        with patch('integrations.orchestration.skillos_stream.APIExecutor',executor), \
             patch('integrations.orchestration.skillos_stream.SkillOSRoles',lambda config:self.roles), \
             patch('integrations.skill_generator.roles.ROOT',root):
            code=await run_stream(args,make_task_set(tasks),output,receipt,root=root)
        self.assertEqual(code,0,receipt)
        self.assertEqual(peak,2)
        self.assertEqual(receipt['actual_task_attempts'],4)
        self.assertEqual(receipt['rewarded_candidates'],2)
        self.assertEqual(len(receipt['fixed_executor_ids']),1)
        self.assertTrue(receipt['resources_closed'])
        library=SkillLibrary(root/'.runtime/skill_library')
        skillsets=[set(library.get(version)) for version in receipt['final_skill_versions']]
        self.assertEqual([len(skills) for skills in skillsets],[2,2])
        self.assertFalse(skillsets[0]&skillsets[1])
        for row in receipt['results']:
            self.assertEqual(row['original_evaluation']['reward'],1)
            self.assertFalse(row['selfjudge_success'])


if __name__=='__main__':unittest.main()
