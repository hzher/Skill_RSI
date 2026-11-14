"""Meaningful model-loop and local-helper boundaries without provider calls."""
from dataclasses import replace
from decimal import InvalidOperation
from copy import deepcopy
import tempfile
from pathlib import Path
import importlib.util
import json
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError
from integrations.model_endpoint import EndpointClient, EndpointError, EndpointSettings
from integrations.model_usage import ModelUsageLedger
from integrations.model_task_executor import (append_response,append_results,calculate,inspect_result,
    measurement_view,model_body,redact,tool_inventory,normalized_result,calculate_batch,argument_error)
from integrations.model_task_executor import local_argument_feedback,recoverable_read_error,VerificationBudget,execute_task,partial_result_view,resolve_local_time,result_envelope
from tests.test_model_endpoint import fixture

try:
    resolve_local_time('2000-01-01T00:00','America/New_York')
    TIME_ZONE_DATA_AVAILABLE=True
except ValueError:
    TIME_ZONE_DATA_AVAILABLE=False


def execution_root(root):
    (root/'configs').mkdir()
    (root/'configs/task_adapter_profiles.json').write_text(json.dumps({'profiles':{'unit':{'task_id':'unit','resource_backends':['isolated']}}}))
    (root/'configs/mainline_sources.lock.json').write_text(json.dumps({'sources':{'toolathlon_gym':{}}}))
    for name in ('integrations/model_task_executor.py','integrations/model_result_analysis.py','integrations/model_stream.py',
                 'integrations/model_endpoint.py','integrations/model_usage.py','integrations/nemo_http.py','scripts/run_model_tasks.py'):
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('source fixture')

class ModelTaskExecutorTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('jsonschema'),'Optional model-task validation dependency')
    def test_external_skill_reaches_model_but_never_grants_unlisted_tools(self):
        from integrations.skill_rsi_contract import skill_version
        case=self
        skill={'schema_version':'skill-v1','skill_id':'fixture','objective':'Read the task data',
               'steps':['Use the granted read tool'],'allowed_tools':['fake__read']}
        class Model:
            requests=0
            def __init__(self,settings,*args,**kwargs):self.settings=settings;self.attempts=0;self.observations=[]
            def request(self,body):
                self.attempts+=1;Model.requests+=1
                if self.attempts==1:
                    case.assertEqual(json.loads(body['input'][-1]['content'].split('\n')[-1]),skill)
                    case.assertEqual({t['name'] for t in body['tools']},{'fake__read','verify_task'})
                    name='fake__write' if mode=='deny_write' else 'fake__read'
                    return {'status':'completed','output':[{'type':'function_call','call_id':'read-id','name':name,'arguments':'{}'}]}
                return {'status':'completed','output':[]}
        class Resource:
            def __init__(self):self.journal=[];self.calls=[];self.closed=False
            def post(self,route,body):
                self.journal.append({'route':route})
                if route=='/isolated/tools':return {'tools':[{'server':'fake','name':name,'inputSchema':{'type':'object'}} for name in ('read','write')]}
                if route=='/isolated/tool':self.calls.append(body['name']);return {'result':{'read':True}}
                if route=='/isolated/verify':return {'result':{'reward':int(self.calls==['read']),'mask_sample':False,'evaluation':{}}}
                if route=='/isolated/close_session':self.closed=True
                return {}
        for mode in ('allowed_read','deny_write','unavailable_tool'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);execution_root(root)
                (root/'integrations/skill_rsi_contract.py').write_text('fixture source')
                current=deepcopy(skill)
                if mode=='unavailable_tool':current['allowed_tools']=['missing_tool']
                resource=Resource();Model.requests=0
                with patch('integrations.model_task_executor.EndpointClient',Model),patch('integrations.model_task_executor.NeMoHTTP',return_value=resource),patch('integrations.model_task_executor.expected_identity',return_value={'fixture':'bound'}),patch('integrations.model_task_executor.pinned_blob',return_value=(b'Public fixture task',{})):
                    result=execute_task(root,'unit',EndpointSettings('openai_responses','https://unit.invalid','unit','unit-secret'),
                        max_steps=2,skill=current,evaluation_scope='skill_rsi')
                self.assertTrue(resource.closed)
                self.assertEqual(result['skill_sha256'],skill_version(current))
                self.assertEqual(resource.calls,['read'] if mode=='allowed_read' else [])
                self.assertEqual(result['status'],'passed' if mode=='allowed_read' else 'unknown')
                if mode=='unavailable_tool':self.assertEqual(Model.requests,0)

    def test_small_result_reference_cannot_be_spoofed_by_source_metadata(self):
        value={'result_ref':'untrusted-reference','stored_complete':False,'rows':[{'value':'3.50'}]}
        original=deepcopy(value)
        envelope=json.loads(result_envelope('native-call-id',value))
        self.assertEqual(envelope['result_ref'],'native-call-id')
        self.assertIs(envelope['stored_complete'],True)
        self.assertEqual(envelope['value'],original)
        self.assertEqual(value,original)

    def test_large_multibyte_result_keeps_reference_without_leaking_a_partial_value(self):
        value={'data':[{'name':'甲🙂'*7000}]}
        envelope=json.loads(result_envelope('native-large-id',value))
        self.assertEqual(envelope['result_ref'],'native-large-id')
        self.assertEqual(envelope['keys'],['data'])
        self.assertEqual(envelope['use_tool'],'inspect_result')
        self.assertNotIn('value',envelope)
        self.assertNotIn('甲',str(envelope))

    @unittest.skipUnless(importlib.util.find_spec('jsonschema'),'Optional model-task validation dependency')
    def test_visible_small_references_chain_read_analysis_and_original_workbook_write(self):
        case=self
        schema={'type':'object','required':['Rows'],'additionalProperties':False,'properties':{'Rows':{
            'type':'array','maxItems':4,'items':{'type':'object','required':['Name','Amount'],'additionalProperties':False,
            'properties':{'Name':{'type':'string'},'Amount':{'type':'number'}}}}}}
        class Model:
            def __init__(self,settings,*args,**kwargs):self.settings=settings;self.attempts=0;self.observations=[]
            def request(self,body):
                self.attempts+=1
                outputs={r['call_id']:json.loads(r['output']) for r in body['input'] if r.get('type')=='function_call_output'}
                if self.attempts==1:
                    identity,name,args='native-source-id','fake__read',{}
                elif self.attempts==2:
                    source=outputs['native-source-id']
                    case.assertEqual(source['result_ref'],'native-source-id')
                    case.assertEqual(source['value']['result_ref'],'untrusted-reference')
                    identity,name,args='native-analysis-id','analyze_results',{
                        'sources':[{'result_ref':source['result_ref'],'path':'/data'}],
                        'group_by':[{'field':'/category','name':'Group'}],
                        'metrics':[{'name':'Sum','op':'sum','field':'/amount','decimals':2}]}
                elif self.attempts==3:
                    aggregate=outputs['native-analysis-id']
                    case.assertEqual(aggregate['value']['rows'],[{'Group':'A','Sum':'3.50'}])
                    identity,name,args='native-write-id','write_workbook_from_results',{'tables':{'Rows':{
                        'sources':[{'result_ref':aggregate['result_ref'],'path':'/rows'}],
                        'columns':{'Name':{'field':'/Group'},'Amount':{'field':'/Sum'}}}}}
                else:return {'status':'completed','output':[]}
                return {'status':'completed','output':[{'type':'function_call','call_id':identity,'name':name,'arguments':json.dumps(args)}]}
        class Resource:
            def __init__(self):self.journal=[];self.writes=[];self.closed=False
            def post(self,route,body):
                self.journal.append({'route':route})
                if route=='/isolated/tools':return {'tools':[{'server':'fake','name':'read','inputSchema':{'type':'object'}}],
                    'artifact_tools':[{'name':'write_workbook','inputSchema':schema}]}
                if route=='/isolated/tool':return {'result':{'result_ref':'untrusted-reference','data':[
                    {'category':'A','amount':'1.20'},{'category':'A','amount':'2.30'}]}}
                if route=='/isolated/write_workbook':self.writes.append(body['workbook']);return {'result':{'written':True}}
                if route=='/isolated/verify':return {'result':{'reward':int(self.writes==[{'Rows':[{'Name':'A','Amount':3.5}]}]),'mask_sample':False,'evaluation':{}}}
                if route=='/isolated/close_session':self.closed=True
                return {}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);execution_root(root);resource=Resource()
            with patch('integrations.model_task_executor.EndpointClient',Model),patch('integrations.model_task_executor.NeMoHTTP',return_value=resource),patch('integrations.model_task_executor.expected_identity',return_value={'fixture':'bound'}),patch('integrations.model_task_executor.pinned_blob',return_value=(b'Public fixture task',{})):
                result=execute_task(root,'unit',EndpointSettings('openai_responses','https://unit.invalid','unit','unit-secret'),max_steps=4)
            self.assertEqual(result['status'],'passed')
            self.assertTrue(resource.closed)
            self.assertEqual(resource.writes,[{'Rows':[{'Name':'A','Amount':3.5}]}])

    @unittest.skipUnless(importlib.util.find_spec('jsonschema'),'Optional model-task validation dependency')
    def test_reference_write_validates_before_io_and_never_retries_unknown_mutation(self):
        schema={'type':'object','required':['Rows'],'additionalProperties':False,'properties':{'Rows':{
            'type':'array','maxItems':4,'items':{'type':'object','required':['Name'],'additionalProperties':False,
            'properties':{'Name':{'type':'string','maxLength':4}}}}}}
        class Model:
            def __init__(self,settings,*args,**kwargs):self.settings=settings;self.attempts=0;self.observations=[]
            def request(self,body):
                self.attempts+=1
                if self.attempts==1:call={'type':'function_call','call_id':'raw','name':'fake__read','arguments':'{}'}
                else:
                    field='/missing' if mode=='invalid_projection' else '/name'
                    truncate=5 if mode=='invalid_schema' else 4
                    plan={'tables':{'Rows':{'sources':[{'result_ref':'raw','path':'/data'}],
                           'columns':{'Name':{'field':field,'truncate':truncate}}}}}
                    call={'type':'function_call','call_id':'write','name':'write_workbook_from_results','arguments':json.dumps(plan)}
                return {'status':'completed','output':[call]}
        class Resource:
            def __init__(self):self.journal=[];self.writes=[];self.closed=False
            def post(self,route,body):
                self.journal.append({'route':route})
                if route=='/isolated/tools':return {'tools':[{'server':'fake','name':'read','inputSchema':{'type':'object'}}],
                    'artifact_tools':[{'name':'write_workbook','inputSchema':schema}]}
                if route=='/isolated/tool':return {'result':{'data':[{'name':'甲乙丙🙂X'}]}}
                if route=='/isolated/write_workbook':
                    self.writes.append(body['workbook'])
                    if mode=='remote_unknown':raise ValueError('Remote parse failed after a possible write')
                    return {'result':{'written':True}}
                if route=='/isolated/verify':return {'result':{'reward':int(bool(self.writes)),'mask_sample':False,'evaluation':{}}}
                if route=='/isolated/close_session':self.closed=True
                return {}
        for mode in ('success','invalid_projection','invalid_schema','remote_unknown'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);execution_root(root);resource=Resource()
                with patch('integrations.model_task_executor.EndpointClient',Model),patch('integrations.model_task_executor.NeMoHTTP',return_value=resource),patch('integrations.model_task_executor.expected_identity',return_value={'fixture':'bound'}),patch('integrations.model_task_executor.pinned_blob',return_value=(b'Public fixture task',{})):
                    result=execute_task(root,'unit',EndpointSettings('openai_responses','https://unit.invalid','unit','unit-secret'),max_steps=2)
                self.assertTrue(resource.closed)
                self.assertEqual(len(resource.writes),0 if mode.startswith('invalid_') else 1)
                if resource.writes:self.assertEqual(resource.writes,[{'Rows':[{'Name':'甲乙丙🙂'}]}])
                self.assertEqual(result['status'],'passed' if mode=='success' else 'unknown' if mode=='remote_unknown' else 'business_failed')

    def test_missing_timezone_data_is_an_explicit_local_failure(self):
        with patch('integrations.model_task_executor.ZoneInfo',side_effect=ZoneInfoNotFoundError):
            with self.assertRaisesRegex(ValueError,'time_zone_unavailable'):
                resolve_local_time('2026-03-09T07:00','America/New_York')

    @unittest.skipUnless(TIME_ZONE_DATA_AVAILABLE,'Optional timezone database unavailable')
    def test_timezone_helper_resolves_dst_without_choosing_a_business_time(self):
        result=resolve_local_time('2026-03-09T07:00:00','America/New_York')
        self.assertEqual(result['local_datetime'],'2026-03-09T07:00:00-04:00')
        self.assertEqual(result['utc_datetime'],'2026-03-09T11:00:00+00:00')
        self.assertFalse(result['ambiguous'])
        self.assertEqual(resolve_local_time('2026-01-09T07:00','America/New_York')['local_datetime'],'2026-01-09T07:00:00-05:00')

    @unittest.skipUnless(TIME_ZONE_DATA_AVAILABLE,'Optional timezone database unavailable')
    def test_timezone_helper_refuses_a_spring_gap(self):
        with self.assertRaisesRegex(ValueError,'time_nonexistent_local_time'):
            resolve_local_time('2026-03-08T02:30','America/New_York')

    @unittest.skipUnless(TIME_ZONE_DATA_AVAILABLE,'Optional timezone database unavailable')
    def test_timezone_helper_requires_a_choice_for_fall_overlap(self):
        with self.assertRaisesRegex(ValueError,'time_ambiguous_local_time_requires_fold'):
            resolve_local_time('2026-11-01T01:30','America/New_York')
        first=resolve_local_time('2026-11-01T01:30','America/New_York',0)
        second=resolve_local_time('2026-11-01T01:30','America/New_York',1)
        self.assertTrue(first['ambiguous']);self.assertNotEqual(first['utc_datetime'],second['utc_datetime'])

    def test_timezone_helper_rejects_offsets_invalid_dates_zones_and_boolean_fold(self):
        for args in [('2026-03-09T07:00-05:00','America/New_York'),('2026-02-30T07:00','UTC'),
                     ('2026-03-09T07:00','../PRIVATE_ZONE'),('2026-03-09T07:00','Missing/Zone'),
                     ('2026-03-09T07:00','UTC',True)]:
            with self.subTest(args=args),self.assertRaises(ValueError):resolve_local_time(*args)
    def test_display_pagination_is_identified_without_guessing_business_completeness(self):
        source={'raw':[{'value':i} for i in range(3)]}
        self.assertTrue(partial_result_view(inspect_result(source,{'result_ref':'raw','limit':2})))
        self.assertTrue(partial_result_view(inspect_result(source,{'result_ref':'raw','offset':2,'limit':2})))
        self.assertFalse(partial_result_view(inspect_result(source,{'result_ref':'raw','limit':3})))
        self.assertFalse(partial_result_view({'items':[],'offset':'PRIVATE','total':None}))
        feedback=local_argument_feedback(ValueError('analysis_partial_view'),'local','analyze_results')
        self.assertIn('original data tool result_ref',feedback['correction'])
    def test_order_feedback_names_the_failed_contract_without_product_or_money_oracles(self):
        observed=measurement_view({'reward':0,'evaluation':{'supplemental':{
            'rows_passed':False,'calendar_passed':True,
            'errors':['complete_low_stock_product_multiset_differs',
                      'complete_low_stock_product_multiset_differs PRIVATE_PRODUCT',
                      'Expected total PRIVATE_AMOUNT'],
            'measurements':{'expected_products':['PRIVATE_PRODUCT'],'total':'PRIVATE_AMOUNT'}}}})
        self.assertEqual(observed['failed_checks'],['complete_low_stock_product_multiset_differs','rows_passed'])
        self.assertNotIn('PRIVATE',str(observed))

    @unittest.skipUnless(importlib.util.find_spec('jsonschema'),'Optional model-task validation dependency')
    def test_model_loop_preserves_final_measurement_after_repeated_failed_checks(self):
        class Model:
            def __init__(self,settings,*args,**kwargs):
                self.settings=settings;self.attempts=0;self.observations=[]
            def request(self,body):
                self.attempts+=1
                calls=[] if self.attempts==11 else [{'type':'function_call','call_id':'check_'+str(self.attempts),'name':'verify_task','arguments':'{}'}]
                return {'status':'completed','output':calls}
        class Resource:
            def __init__(self,final_reward):self.journal=[];self.checks=0;self.final_reward=final_reward;self.closed=False
            def post(self,route,body):
                self.journal.append({'route':route})
                if route=='/isolated/verify':
                    self.checks+=1
                    if self.checks>10:raise RuntimeError('The unchanged environment refuses an eleventh check')
                    return {'result':{'reward':self.final_reward if self.checks==10 else 0,'mask_sample':False,'evaluation':{}}}
                if route=='/isolated/close_session':self.closed=True
                return {}
        for final_reward in (0,1):
            with self.subTest(final_reward=final_reward),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);execution_root(root)
                resource=Resource(final_reward)
                with patch('integrations.model_task_executor.EndpointClient',Model),patch('integrations.model_task_executor.NeMoHTTP',return_value=resource),patch('integrations.model_task_executor.expected_identity',return_value={'fixture':'bound'}),patch('integrations.model_task_executor.pinned_blob',return_value=(b'Public fixture task',{})):
                    result=execute_task(root,'unit',EndpointSettings('openai_responses','https://unit.invalid','unit','unit-secret'),max_steps=12)
                self.assertEqual(resource.checks,10)
                self.assertTrue(resource.closed)
                self.assertEqual(result['model_http_requests'],11)
                self.assertEqual(result['status'],'passed' if final_reward else 'business_failed')
                self.assertIsNone(result['failure_kind'])
                self.assertEqual(result['task_verifications'],10)

    def test_model_checks_cannot_consume_the_reserved_final_verification(self):
        budget=VerificationBudget()
        for expected in range(8,-1,-1):
            self.assertTrue(budget.take())
            self.assertEqual(budget.remaining_model_checks,expected)
        for _ in range(3):self.assertFalse(budget.take())
        self.assertEqual(budget.calls,9)
        self.assertTrue(budget.take(final=True))
        self.assertEqual(budget.calls,10)
        self.assertFalse(budget.take(final=True))
        self.assertFalse(budget.take())
        self.assertEqual(budget.calls,10)

    def test_fixed_nested_meal_error_categories_are_useful_without_answer_values(self):
        observed=measurement_view({'reward':0,'evaluation':{'supplemental':{
            'errors':['invalid_day','recipe_name_not_in_public_snapshot','invalid_day'],
            'measurements':{'correct_recipe':'PRIVATE_RECIPE','expected_date':'PRIVATE_DATE'}}}})
        self.assertEqual(observed['failed_checks'],['invalid_day','recipe_name_not_in_public_snapshot'])
        self.assertNotIn('PRIVATE',str(observed))

    def test_verifier_error_strings_require_exact_allowlisted_categories(self):
        observed=measurement_view({'reward':0,'evaluation':{'errors':[
            'invalid_day PRIVATE_DATE','recipe_name_not_in_public_snapshot:PRIVATE_RECIPE',
            'state_MT_counts_sales_tax',{'invalid_day':'PRIVATE_DATE'},'unknown_category'],
            'supplemental':{'errors':'invalid_day'}}})
        self.assertEqual(observed['failed_checks'],[])

    def test_absent_workbook_is_a_recoverable_read_without_weakening_writes(self):
        row={'route':'/isolated/read_workbook','status':400,'response':{'detail':'Owned workbook missing or exceeds 1 MiB'}}
        result=recoverable_read_error('/read_workbook',row)
        self.assertEqual(result['result']['tool_read_error'],'artifact_not_readable')
        self.assertIsNone(recoverable_read_error('/write_workbook',row))

    def test_transport_and_unknown_read_errors_never_receive_local_recovery(self):
        for row in ({'route':'/isolated/read_workbook','status':503,'response':{'detail':'ResourceError'}},
                    {'route':'/isolated/read_workbook','status':400,'response':{'detail':'Unknown ownership'}},None):
            with self.subTest(row=row):self.assertIsNone(recoverable_read_error('/read_workbook',row))
    def test_valid_ten_call_response_keeps_all_native_ids_and_result_order(self):
        content=[{'type':'tool_use','id':'call_'+str(i),'name':'list_rows','input':{'page':i+1}} for i in range(10)]
        messages=[]
        calls=append_response('anthropic_messages',messages,{'content':content,'stop_reason':'tool_use'})
        self.assertEqual(len(calls),10)
        append_results('anthropic_messages',messages,[(c['id'],'{}') for c in calls])
        self.assertEqual([r['tool_use_id'] for r in messages[-1]['content']],[c['id'] for c in calls])
        self.assertEqual(messages[0]['content'],content)

    def test_oversized_tool_batch_has_a_distinct_budget_failure(self):
        content=[{'type':'tool_use','id':'call_'+str(i),'name':'list_rows','input':{}} for i in range(65)]
        with self.assertRaises(EndpointError) as caught:append_response('anthropic_messages',[],{'content':content,'stop_reason':'tool_use'})
        self.assertEqual(caught.exception.code,'tool_batch_budget_exhausted')
    def test_remote_json_errors_never_become_retryable_model_arguments(self):
        for kind,name in (('mcp','read'),('artifact','write_workbook'),('local','verify_task')):
            error=ValueError('Remote response parse failed after possible mutation')
            with self.subTest(kind=kind,name=name),self.assertRaises(ValueError) as caught:
                local_argument_feedback(error,kind,name)
            self.assertIs(caught.exception,error)

    def test_local_feedback_keeps_fixed_codes_without_private_error_values(self):
        self.assertEqual(local_argument_feedback(ValueError('analysis_labels'),'local','analyze_results')['argument_error_code'],'analysis_labels')
        result=local_argument_feedback(KeyError('PRIVATE_DATA'),'local','inspect_result')
        self.assertEqual(result['argument_error_code'],'unknown_result_reference_or_json_pointer')
        self.assertNotIn('PRIVATE_DATA',str(result))
        self.assertNotIn('PRIVATE_DATA',str(local_argument_feedback(ValueError('PRIVATE_DATA'),'local','calculate')))

    def test_array_path_feedback_explains_root_without_business_data(self):
        result=local_argument_feedback(ValueError('analysis_index'),'local','analyze_results')
        self.assertIn('path=""',result['correction'])
        self.assertIn('/ is not the array root',result['correction'])
    def test_nested_order_and_alias_feedback_is_boolean_only(self):
        view=measurement_view({'reward':0,'evaluation':{'original_passed':False,'supplemental':{
            'rows_passed':True,'calendar_passed':False,'errors':['Expected private value 12345'],
            'measurements':{'private_expected':12345}}}})
        self.assertIs(view['upstream_passed'],False)
        self.assertEqual(view['failed_checks'],['calendar_passed'])
        self.assertNotIn('12345',str(view))

    def test_decimal_calculator_rounds_without_float_drift(self):
        self.assertEqual(calculate('round(sum(["1.005", "2.00"]), 2)'),{'value':'3.01'})
        self.assertEqual(calculate('round(100 * 3 / 7, 2)'),{'value':'42.86'})

    def test_calculator_has_no_python_execution_or_file_access(self):
        for text in ('__import__("os")','open("x")','(1).__class__','[x for x in [1]]','2**999999999','"NaN"'):
            with self.subTest(text=text), self.assertRaises((ValueError,InvalidOperation)): calculate(text)

    def test_batch_calculation_keeps_labels_precision_and_single_call(self):
        result=calculate_batch({'sales':'sum(["1.005", "2.00"])','rounded':'round("1.005",2)'})
        self.assertEqual(result,{'values':{'sales':'3.005','rounded':'1.01'}})

    def test_batch_calculation_is_bounded_and_never_executes_code(self):
        for value in ({}, {'x':'open("secret")'}, {str(i):'1' for i in range(101)}, {'x':True}):
            with self.subTest(value=value),self.assertRaises((ValueError,SyntaxError)): calculate_batch(value)

    def test_saved_outputs_can_be_projected_without_changing_source(self):
        values={'call1':{'data':[{'id':1,'price':'10','private_extra':'a'},{'id':2,'price':'20'}]}}
        original=deepcopy(values)
        result=inspect_result(values,{'result_ref':'call1','path':'/data','offset':0,'limit':1,'fields':['id','price']})
        self.assertEqual(result,{'total':2,'offset':0,'has_more':True,'items':[{'id':1,'price':'10'}]})
        self.assertEqual(values,original)

    def test_output_inspector_rejects_unknown_reference_and_bad_bounds(self):
        for args in ({'result_ref':'no'},{'result_ref':'x','limit':True},{'result_ref':'x','offset':-1},{'result_ref':'x','path':'bad'}):
            with self.subTest(args=args),self.assertRaises((ValueError,KeyError)): inspect_result({'x':[]},args)

    def test_tool_mapping_uses_only_discovery_and_rejects_collision(self):
        discovery={'tools':[{'server':'woocommerce','name':'list','inputSchema':{'type':'object'}}],
                   'artifact_tools':[{'name':'write_workbook','inputSchema':{'type':'object'}}]}
        tools,mapping=tool_inventory(discovery)
        self.assertEqual(mapping['woocommerce__list'],('mcp','woocommerce','list'))
        self.assertIn('calculate',mapping)
        discovery['tools'].append(deepcopy(discovery['tools'][0]))
        with self.assertRaises(ValueError): tool_inventory(discovery)

    def test_claude_preserves_signed_thinking_and_all_tool_results(self):
        messages=[{'role':'user','content':'fixture'}]
        response=fixture('anthropic_messages',1)
        calls=append_response('anthropic_messages',messages,response)
        self.assertEqual(messages[-1]['content'],response['content'])
        append_results('anthropic_messages',messages,[(calls[0]['id'],'{"value":5}')])
        self.assertEqual(messages[-1]['content'][0]['tool_use_id'],'toolu-unit')

    def test_responses_preserves_reasoning_and_correlates_output(self):
        messages=[]
        calls=append_response('openai_responses',messages,fixture('openai_responses',1))
        self.assertEqual(messages[0]['encrypted_content'],'unit-ciphertext')
        append_results('openai_responses',messages,[(calls[0]['id'],'5')])
        self.assertEqual(messages[-1]['call_id'],'call-unit-1')

    def test_unknown_or_incomplete_model_output_does_not_execute_tools(self):
        value=fixture('openai_responses',1); value['status']='incomplete'
        with self.assertRaises(EndpointError): append_response('openai_responses',[],value)

    def test_duplicate_tool_call_ids_are_rejected(self):
        value=fixture('anthropic_messages',1)
        value['content'].append(deepcopy(value['content'][-1]))
        with self.assertRaises(EndpointError): append_response('anthropic_messages',[],value)

    def test_measurement_does_not_send_oracle_or_private_paths_to_model(self):
        value={'reward':0,'mask_sample':False,'evaluation':{'upstream_passed':False,'supplemental_passed':False,'expected_answer':'secret'},'run_ref':'private'}
        observed=measurement_view(value)
        self.assertNotIn('secret',str(observed)); self.assertNotIn('private',str(observed))

    def test_measurement_returns_failed_names_without_expected_numbers(self):
        value={'reward':0,'evaluation':{'supplemental_checks':[{'name':'overall_Total Sales','passed':False,'detail':{'expected':123456}}]}}
        observed=measurement_view(value)
        self.assertEqual(observed['failed_checks'],['overall_Total Sales'])
        self.assertNotIn('123456',str(observed))

    def test_dynamic_oracle_row_identities_are_not_feedback(self):
        value={'reward':0,'evaluation':{'supplemental_checks':[{'name':'state_MT_counts_sales_tax','passed':False}]}}
        observed=measurement_view(value)
        self.assertEqual(observed['failed_checks'],['state_constraint'])
        self.assertNotIn('MT',str(observed))

    @unittest.skipUnless(importlib.util.find_spec('jsonschema'), 'Optional model-task validation dependency')
    def test_invalid_artifact_is_rejected_before_http_mutation(self):
        schema={'type':'object','properties':{'rows':{'type':'array'}},'required':['rows'],'additionalProperties':False}
        result=argument_error(schema,{'Overall':{'placeholder':'ignore'}})
        self.assertEqual(result['tool_argument_error'],'schema_validation_failed')
        self.assertIn('not executed',result['retry_guidance'])
        self.assertIsNone(argument_error(schema,{'rows':[]}))

    def test_trace_redacts_both_known_and_key_shaped_values(self):
        value={'body':'unit-secret and sk-'+'a'*24,'nested':['unit-secret']}
        result=redact(value,'unit-secret')
        self.assertNotIn('unit-secret',str(result)); self.assertNotIn('sk-',str(result))

    def test_request_budget_extension_is_explicit_and_still_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=EndpointSettings('openai_responses','https://unit.invalid','unit','unit-secret')
            ledger=ModelUsageLedger(Path(tmp))
            client=EndpointClient(cfg,ledger,max_requests=100,max_request_bytes=4*1024*1024)
            client.attempts=100
            with self.assertRaises(EndpointError): client.request({})
            with self.assertRaises(EndpointError): EndpointClient(cfg,ledger,max_requests=101)

    def test_real_task_tools_do_not_force_claude_55(self):
        cfg=EndpointSettings('anthropic_messages','https://unit.invalid','claude-opus-5-5','unit-secret')
        body=model_body(cfg,[{'name':'unit','description':'test','parameters':{'type':'object'}}],[])
        self.assertEqual(body['tool_choice']['type'],'auto')
        self.assertNotIn('thinking',body)

    def test_mcp_payload_decodes_only_literal_json(self):
        self.assertEqual(normalized_result({'result':{'content':[{'type':'text','text':'{"x":2}'}]}}),{'isError':False,'data':{'x':2}})

if __name__=='__main__': unittest.main()
