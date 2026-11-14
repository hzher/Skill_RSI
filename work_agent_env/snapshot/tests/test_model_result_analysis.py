"""Observed-row analysis preserves data and exposes no file or evaluator access."""
from copy import deepcopy
import unittest
from integrations.model_result_analysis import analyze_results,pointer,materialize_workbook,workbook_reference_tool,ProjectionError


class ModelResultAnalysisTests(unittest.TestCase):
    def workbook_fixture(self):
        columns={'Name':{'type':'string'},'SKU':{'type':['string','null']},'Stock':{'type':'number'},'Price':{'type':'number'}}
        schema={'type':'object','required':['Report'],'additionalProperties':False,'properties':{'Report':{
            'type':'array','maxItems':10,'items':{'type':'object','required':list(columns),'additionalProperties':False,'properties':columns}}}}
        results={'source':{'data':[{'name':'甲乙丙🙂X','sku':None,'stock':2,'price':'1.25'},
                                   {'name':'BetaLong','sku':'B','stock':1,'price':'2.50'},
                                   {'name':'Skip','sku':'C','stock':None,'price':'9'}]}}
        plan={'tables':{'Report':{'sources':[{'result_ref':'source','path':'/data'}],
            'columns':{'Name':{'field':'/name','truncate':4},'SKU':{'field':'/sku'},'Stock':{'field':'/stock'},'Price':{'field':'/price'}},
            'filters':[{'field':'/stock','op':'ne','value':None},{'field':'/stock','op':'lt','value':3}],
            'sort':[{'column':'Stock'},{'column':'Name'}]}}}
        return results,plan,schema

    def test_reference_workbook_projects_exact_unicode_and_numbers_without_task_logic(self):
        results,plan,schema=self.workbook_fixture();original=deepcopy((results,plan,schema))
        output=materialize_workbook(results,plan,schema)
        self.assertEqual(output,{'Report':[{'Name':'Beta','SKU':'B','Stock':1,'Price':2.5},
                                          {'Name':'甲乙丙🙂','SKU':None,'Stock':2,'Price':1.25}]})
        self.assertEqual((results,plan,schema),original)
        tool=workbook_reference_tool(schema)
        self.assertEqual(tool['parameters']['properties']['tables']['required'],['Report'])
        self.assertIsNone(workbook_reference_tool({'type':'object','properties':{'Nested':{'type':'array','items':{'type':'array'}}}}))

    def test_reference_workbook_refuses_unknown_partial_and_duplicate_inputs(self):
        results,plan,schema=self.workbook_fixture()
        with self.assertRaisesRegex(ValueError,'partial_view'):materialize_workbook(results,plan,schema,partial_views={'source'})
        plan['tables']['Report']['sources'].append(plan['tables']['Report']['sources'][0])
        with self.assertRaisesRegex(ValueError,'duplicate_source'):materialize_workbook(results,plan,schema)
        plan['tables']['Report']['sources']=[{'result_ref':'../../PRIVATE_PATH'}]
        with self.assertRaisesRegex(ProjectionError,'reference_missing'):materialize_workbook(results,plan,schema)

    def test_projection_field_feedback_lists_observed_keys_without_row_values(self):
        results,plan,schema=self.workbook_fixture()
        results['source']['data'][0]['name']='PRIVATE_ROW_VALUE'
        plan['tables']['Report']['columns']['Name']['field']='/Name'
        with self.assertRaises(ProjectionError) as caught:materialize_workbook(results,plan,schema)
        feedback=caught.exception.feedback
        self.assertEqual(feedback['argument_error_code'],'analysis_workbook_field_missing')
        self.assertEqual(feedback['column'],'Name')
        self.assertEqual(feedback['source_field'],'/Name')
        self.assertIn('name',feedback['available_fields'])
        self.assertNotIn('PRIVATE_ROW_VALUE',str(feedback))

    def test_projection_numeric_mapping_and_null_filter_errors_are_distinct(self):
        results,plan,schema=self.workbook_fixture()
        plan['tables']['Report']['columns']['Price']['field']='/name'
        with self.assertRaises(ProjectionError) as caught:materialize_workbook(results,plan,schema)
        self.assertEqual(caught.exception.feedback['argument_error_code'],'analysis_workbook_numeric_mapping')
        self.assertEqual(caught.exception.feedback['column'],'Price')
        self.assertEqual(caught.exception.feedback['expected_type'],['number'])
        plan['tables']['Report']['columns']['Price']['field']='/price'
        plan['tables']['Report']['filters']=plan['tables']['Report']['filters'][1:]
        with self.assertRaises(ProjectionError) as caught:materialize_workbook(results,plan,schema)
        self.assertEqual(caught.exception.feedback['argument_error_code'],'analysis_workbook_filter_input')
        self.assertEqual(caught.exception.feedback['source_field'],'/stock')

    def test_projection_source_path_error_reveals_structure_not_container_values(self):
        results,plan,schema=self.workbook_fixture()
        plan['tables']['Report']['sources'][0]['path']='/missing'
        with self.assertRaises(ProjectionError) as caught:materialize_workbook(results,plan,schema)
        self.assertEqual(caught.exception.feedback['available_fields'],['data'])
        self.assertNotIn('BetaLong',str(caught.exception.feedback))

    def test_reference_workbook_never_silently_rounds_numeric_cells(self):
        results,plan,schema=self.workbook_fixture();results['source']['data'][0]['price']='0.123456789123456789'
        with self.assertRaisesRegex(ValueError,'lossy_number'):materialize_workbook(results,plan,schema)

    def test_reference_workbook_requires_explicit_text_slicing_and_budgeted_rows(self):
        results,plan,schema=self.workbook_fixture();del plan['tables']['Report']['columns']['Name']['truncate']
        self.assertEqual(materialize_workbook(results,plan,schema)['Report'][0]['Name'],'BetaLong')
        schema['properties']['Report']['maxItems']=1
        with self.assertRaisesRegex(ValueError,'row_budget'):materialize_workbook(results,plan,schema)

    def setUp(self):
        self.results={'p1':{'data':[{'group':{'key':'A'},'status':'ok','amount':'1.005'},
                                    {'group':{'key':'B'},'status':'skip','amount':'100'}]},
                      'p2':{'data':[{'group':{'key':'A'},'status':'ok','amount':'2.005'},
                                    {'group':{'key':'B'},'status':'ok','amount':'4.00'}]}}
        self.args={'sources':[{'result_ref':p,'path':'/data'} for p in ('p1','p2')],
                   'filters':[{'field':'/status','op':'eq','value':'ok'}],
                   'group_by':[{'field':'/group/key','name':'Group'}],
                   'metrics':[{'name':'Count','op':'count'},{'name':'Amount','op':'sum','field':'/amount','decimals':2},
                              {'name':'Average','op':'mean','field':'/amount','decimals':2}],
                   'sort':[{'name':'Amount','descending':True}]}

    def test_complete_pages_nested_groups_decimal_sums_and_filters(self):
        result=analyze_results(self.results,self.args)
        self.assertEqual((result['input_rows'],result['selected_rows'],result['source_count']),(4,3,2))
        self.assertEqual(result['rows'],[{'Group':'B','Count':1,'Amount':'4.00','Average':'4.00'},
                                         {'Group':'A','Count':2,'Amount':'3.01','Average':'1.51'}])

    def test_analysis_never_mutates_sources_or_the_model_plan(self):
        original=deepcopy((self.results,self.args));analyze_results(self.results,self.args)
        self.assertEqual((self.results,self.args),original)

    def test_local_partial_view_is_rejected_without_expanding_or_changing_inputs(self):
        self.results['view']={'items':self.results['p1']['data'][:1],'total':2,'offset':0,'has_more':True}
        args=deepcopy(self.args);args['sources']=[{'result_ref':'view','path':'/items'}]
        original=deepcopy((self.results,args))
        with self.assertRaisesRegex(ValueError,'analysis_partial_view'):
            analyze_results(self.results,args,partial_views={'view'})
        self.assertEqual((self.results,args),original)
        self.assertEqual(analyze_results(self.results,self.args,partial_views={'view'})['input_rows'],4)

    def test_bare_row_fields_match_json_pointers_for_groups_filters_and_metrics(self):
        expected=analyze_results(self.results,self.args)
        for item in self.args['group_by']+self.args['filters']+self.args['metrics']:
            if 'field' in item:item['field']=item['field'].removeprefix('/')
        self.assertEqual(analyze_results(self.results,self.args),expected)

    def test_empty_source_has_count_and_sum_but_unknown_mean(self):
        result=analyze_results({'empty':[]},{'sources':[{'result_ref':'empty'}],
            'metrics':[{'name':'Count','op':'count'},{'name':'Sum','op':'sum','field':'/x'},
                       {'name':'Average','op':'mean','field':'/x'}]})
        self.assertEqual(result['rows'],[{'Count':0,'Sum':'0','Average':None}])

    def test_single_partial_page_does_not_claim_complete_source(self):
        args=deepcopy(self.args);args['sources']=args['sources'][:1]
        result=analyze_results(self.results,args)
        self.assertEqual(result['input_rows'],2)
        self.assertIn('no_source_completeness_claim',result['scope'])

    def test_duplicating_a_page_cannot_silently_double_count(self):
        self.args['sources'].append(self.args['sources'][0])
        with self.assertRaisesRegex(ValueError,'duplicate_source'):analyze_results(self.results,self.args)

    def test_nonfinite_boolean_and_oversized_numbers_are_not_amounts(self):
        for value in (True,'NaN','Infinity','1e31','1e-100'):
            self.results['p1']['data'][0]['amount']=value
            with self.subTest(value=value),self.assertRaises(ValueError):analyze_results(self.results,self.args)

    def test_numeric_comparison_and_membership_are_explicit(self):
        self.args['filters']=[{'field':'/amount','op':'gte','value':'2'},{'field':'/status','op':'in','value':['ok']}]
        self.assertEqual(analyze_results(self.results,self.args)['selected_rows'],2)

    def test_hidden_references_missing_fields_and_code_are_not_executed(self):
        for ref,path in [('unknown',''),('p1','/../../etc/passwd'),('p1','__import__("os")')]:
            args=deepcopy(self.args);args['sources']=[{'result_ref':ref,'path':path}]
            with self.subTest(ref=ref,path=path),self.assertRaises((KeyError,ValueError)):analyze_results(self.results,args)

    def test_name_collisions_and_unsupported_metrics_are_rejected(self):
        for metric in ({'name':'Group','op':'count'},{'name':'Bad','op':'exec','field':'/amount'}):
            args=deepcopy(self.args);args['metrics']=[metric]
            with self.subTest(metric=metric),self.assertRaises(ValueError):analyze_results(self.results,args)

    def test_row_and_group_output_budgets_are_enforced(self):
        args={'sources':[{'result_ref':'x'}],'metrics':[{'name':'Count','op':'count'}]}
        with self.assertRaisesRegex(ValueError,'analysis_rows'):analyze_results({'x':[{}]*10001},args)
        args['group_by']=[{'field':'/name','name':'Name'}]
        with self.assertRaisesRegex(ValueError,'output_budget'):
            analyze_results({'x':[{'name':str(i)+'x'*4090} for i in range(80)]},args)

    def test_pointer_preserves_literal_escaped_keys(self):
        self.assertEqual(pointer({'a/b':{'~':[1]}},'/a~1b/~0/0'),1)
        with self.assertRaises(ValueError):pointer([1],'/01')


if __name__=='__main__':unittest.main()
