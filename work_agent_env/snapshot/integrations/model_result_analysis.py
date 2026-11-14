"""Bounded declarative analysis of observed tool rows, without task logic or I/O."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from copy import deepcopy
import json
import re

MAX_ROWS=10000
NAME=r'[A-Za-z][A-Za-z0-9_]{0,63}'


class ProjectionError(ValueError):
    """Repair hints from selected observed structure and the granted schema only."""
    def __init__(self,code,*,table,column=None,source_field=None,observed=None,expected_type=None):
        super().__init__(code)
        self.feedback={'tool_argument_error':'reference_projection_failed','argument_error_code':code,'table':table,
            'retry_guidance':'Inspect the selected result, then map its actual field names. Projection copies rows; compute aggregates with analyze_results first. No workbook write was sent.'}
        if column is not None:self.feedback['column']=column
        if source_field is not None:self.feedback['source_field']=source_field
        if isinstance(observed,dict):
            self.feedback['available_fields']=[key for key in observed if isinstance(key,str) and len(key)<=128][:32]
        if expected_type is not None:self.feedback['expected_type']=expected_type
        if code=='analysis_workbook_filter_input':
            self.feedback['retry_guidance']='Use a numeric source field for numeric comparisons. Exclude nulls explicitly with a preceding ne-null filter when appropriate. No workbook write was sent.'


def pointer(value,path):
    if not isinstance(path,str) or len(path)>512 or (path and not path.startswith('/')):
        raise ValueError('analysis_pointer')
    for part in path.split('/')[1:]:
        part=part.replace('~1','/').replace('~0','~')
        if isinstance(value,list):
            if not re.fullmatch(r'0|[1-9][0-9]*',part):raise ValueError('analysis_index')
            value=value[int(part)]
        else:value=value[part]
    return value


def number(value):
    if type(value) not in (int,float,str,Decimal) or len(str(value))>80:raise ValueError('analysis_number')
    try:result=Decimal(str(value))
    except InvalidOperation:raise ValueError('analysis_number') from None
    if not result.is_finite() or abs(result)>Decimal('1e30') or abs(result.as_tuple().exponent)>40:
        raise ValueError('analysis_number')
    return result


def field_value(row,field):
    # Row fields are JSON paths, never filesystem paths. Accept the familiar
    # "nested/field" spelling as an alias for "/nested/field".
    if isinstance(field,str) and field and not field.startswith('/'):field='/'+field
    return pointer(row,field)


def matches(row,condition):
    if set(condition)!={'field','op','value'}:raise ValueError('analysis_filter')
    value=field_value(row,condition['field']);op=condition['op'];right=condition['value']
    if op in ('eq','ne'):
        same=type(value) is type(right) and value==right
        return same if op=='eq' else not same
    if op in ('in','not_in'):
        if not isinstance(right,list) or len(right)>100 or any(type(v) not in (str,int,float,bool,type(None)) for v in right):
            raise ValueError('analysis_filter_values')
        same=any(type(value) is type(v) and value==v for v in right)
        return same if op=='in' else not same
    if op not in ('lt','lte','gt','gte'):raise ValueError('analysis_filter_operator')
    left,right=number(value),number(right)
    return {'lt':left<right,'lte':left<=right,'gt':left>right,'gte':left>=right}[op]


def analyze_results(results,arguments,*,partial_views=frozenset()):
    if not isinstance(arguments,dict) or set(arguments)-{'sources','filters','group_by','metrics','sort'}:
        raise ValueError('analysis_arguments')
    sources=arguments.get('sources');metrics=arguments.get('metrics');groups=arguments.get('group_by',[])
    filters=arguments.get('filters',[]);sort=arguments.get('sort',[])
    for value,limit in ((sources,64),(metrics,32),(groups,8),(filters,16),(sort,8)):
        if not isinstance(value,list) or len(value)>limit:raise ValueError('analysis_budget')
    if not sources or not metrics:raise ValueError('analysis_required')
    labels=[]
    for group in groups:
        if set(group)!={'field','name'}:raise ValueError('analysis_group')
        labels.append(group['name'])
    for metric in metrics:
        if set(metric)-{'name','op','field','decimals'} or not {'name','op'}<=set(metric):raise ValueError('analysis_metric')
        if metric['op'] not in ('count','sum','mean','min','max'):raise ValueError('analysis_metric_operator')
        if metric['op']!='count' and 'field' not in metric:raise ValueError('analysis_metric_field')
        if metric['op']=='count' and 'field' in metric:raise ValueError('analysis_count_has_no_field')
        if 'decimals' in metric and (type(metric['decimals']) is not int or not 0<=metric['decimals']<=6):raise ValueError('analysis_rounding')
        labels.append(metric['name'])
    if any(not isinstance(name,str) or not re.fullmatch(NAME,name) for name in labels) or len(set(labels))!=len(labels):
        raise ValueError('analysis_labels')
    for item in sort:
        if set(item)-{'name','descending'} or item.get('name') not in labels or type(item.get('descending',False)) is not bool:
            raise ValueError('analysis_sort')
    rows=[];seen=set()
    for source in sources:
        if set(source)-{'result_ref','path'} or not isinstance(source.get('result_ref'),str):raise ValueError('analysis_source')
        identity=(source['result_ref'],source.get('path',''))
        if identity[0] in partial_views:raise ValueError('analysis_partial_view')
        if identity in seen:raise ValueError('analysis_duplicate_source')
        seen.add(identity)
        value=pointer(results[identity[0]],identity[1])
        if not isinstance(value,list) or len(rows)+len(value)>MAX_ROWS or any(not isinstance(row,dict) for row in value):
            raise ValueError('analysis_rows')
        rows.extend(value)
    buckets={};selected=0
    for row in rows:
        if not all(matches(row,condition) for condition in filters):continue
        key=tuple(field_value(row,g['field']) for g in groups)
        if any(type(v) not in (str,int,float,bool,type(None)) or isinstance(v,str) and len(v)>4096 for v in key):
            raise ValueError('analysis_group_value')
        # JSON key encoding distinguishes booleans from integer group values.
        identity=json.dumps(key,allow_nan=False,separators=(',',':'))
        if identity not in buckets:
            if len(buckets)>=1000:raise ValueError('analysis_group_budget')
            buckets[identity]=(key,[])
        buckets[identity][1].append(row);selected+=1
    if not groups and not buckets:buckets['[]']=((),[])
    output=[]
    with localcontext() as context:
        context.prec=96
        for key,members in buckets.values():
            out={group['name']:value for group,value in zip(groups,key)}
            for metric in metrics:
                op=metric['op']
                if op=='count':value=len(members)
                else:
                    values=[number(field_value(row,metric['field'])) for row in members]
                    if op=='sum':value=sum(values,Decimal(0))
                    elif not values:value=None
                    elif op=='mean':value=sum(values,Decimal(0))/len(values)
                    else:value=min(values) if op=='min' else max(values)
                    if value is not None and 'decimals' in metric:
                        value=value.quantize(Decimal(1).scaleb(-metric['decimals']),rounding=ROUND_HALF_UP)
                out[metric['name']]=value
            output.append(out)
        try:
            for item in reversed(sort):output.sort(key=lambda row:(row[item['name']] is None,row[item['name']]),reverse=item.get('descending',False))
        except TypeError:raise ValueError('analysis_sort_type') from None
        output=[{key:str(value) if isinstance(value,Decimal) else value for key,value in row.items()} for row in output]
    report={'input_rows':len(rows),'selected_rows':selected,'source_count':len(sources),'group_count':len(output),'rows':output,
            'decimal_results_are_strings':True,'scope':'observed_tool_rows_only_no_source_completeness_claim'}
    if len(json.dumps(report,allow_nan=False).encode())>256*1024:raise ValueError('analysis_output_budget')
    return report


TOOL={'name':'analyze_results','description':
      'Read-only grouped analysis of COMPLETE saved tool result arrays. Supply all fetched pages as result_ref/path sources; '
      'Use ORIGINAL data tool result references, not local inspect_result /items display pages. Do not repeat pages. '
      'Source paths are JSON pointers: use empty string for a root array, /data for a data array; / is NOT the root. '
      'Row fields accept /billing/state or billing/state, /total or total. Choose filters, group_by, metrics and sort. '
      'No code, filesystem, hidden data or automatic task logic. Count counts rows. Sum/mean/min/max use exact decimals; '
      'rounded decimal results are strings. Fetch all pages first; this tool cannot infer missing records.',
      'parameters':{'type':'object','additionalProperties':False,'required':['sources','metrics'],'properties':{
          'sources':{'type':'array','minItems':1,'maxItems':64,'items':{'type':'object','additionalProperties':False,
                     'required':['result_ref'],'properties':{'result_ref':{'type':'string'},'path':{'type':'string','maxLength':512,'pattern':r'^(?:/|$)'}}}},
          'filters':{'type':'array','maxItems':16,'items':{'type':'object','additionalProperties':False,'required':['field','op','value'],
                     'properties':{'field':{'type':'string'},'op':{'enum':['eq','ne','in','not_in','lt','lte','gt','gte']},'value':{}}}},
          'group_by':{'type':'array','maxItems':8,'items':{'type':'object','additionalProperties':False,'required':['field','name'],
                     'properties':{'field':{'type':'string'},'name':{'type':'string','pattern':'^'+NAME+'$','maxLength':64}}}},
          'metrics':{'type':'array','minItems':1,'maxItems':32,'items':{'type':'object','additionalProperties':False,'required':['name','op'],
                     'properties':{'name':{'type':'string','pattern':'^'+NAME+'$','maxLength':64},'op':{'enum':['count','sum','mean','min','max']},'field':{'type':'string'},
                                   'decimals':{'type':'integer','minimum':0,'maximum':6}}}},
          'sort':{'type':'array','maxItems':8,'items':{'type':'object','additionalProperties':False,'required':['name'],
                  'properties':{'name':{'type':'string'},'descending':{'type':'boolean'}}}}
      }}}


def workbook_reference_tool(schema):
    """Offer references only for discovered flat, typed workbook row tables."""
    properties=schema.get('properties',{})
    if schema.get('type')!='object' or not properties or len(properties)>8:return None
    tables={}
    selection={'type':'object','additionalProperties':False,'required':['field'],'properties':{
        'field':{'type':'string','maxLength':512},'truncate':{'type':'integer','minimum':1,'maximum':4096}}}
    for name,table in properties.items():
        item=table.get('items',{});columns=item.get('properties',{})
        if table.get('type')!='array' or item.get('type')!='object' or not columns or len(columns)>32:return None
        for column in columns.values():
            types=column.get('type');types=[types] if isinstance(types,str) else types
            if not isinstance(types,list) or not types or set(types)-{'string','number','integer','boolean','null'}:return None
        tables[name]={'type':'object','additionalProperties':False,'required':['sources','columns'],'properties':{
            'sources':deepcopy(TOOL['parameters']['properties']['sources']),
            'columns':{'type':'object','additionalProperties':False,'required':item.get('required',[]),
                       'properties':{key:deepcopy(selection) for key in columns}},
            'filters':deepcopy(TOOL['parameters']['properties']['filters']),
            'sort':{'type':'array','maxItems':8,'items':{'type':'object','additionalProperties':False,'required':['column'],
                    'properties':{'column':{'enum':list(columns)},'descending':{'type':'boolean'}}}}}}
    return {'name':'write_workbook_from_results','description':
        'Write the SAME task workbook using references to already observed complete data arrays. '
        'You choose each table source, output column JSON paths, filters, ordering and optional exact character truncation. '
        'Use analyzed result /rows for computed tables; numeric strings are converted only when exact. '
        'No hidden data, task logic, guessed values or automatic business repair. Original workbook schema still applies. '
        'Prefer this over copying long names or numeric tables into literal write_workbook arguments.',
        'parameters':{'type':'object','additionalProperties':False,'required':['tables'],'properties':{
            'tables':{'type':'object','additionalProperties':False,'required':schema.get('required',[]),'properties':tables}}}}


def materialize_workbook(results,arguments,schema,*,partial_views=frozenset()):
    """Pure projection. Callers validate the final literal payload before I/O."""
    if set(arguments)!={'tables'} or not isinstance(arguments['tables'],dict):raise ValueError('analysis_workbook_arguments')
    if workbook_reference_tool(schema) is None:raise ValueError('analysis_workbook_schema')
    output={}
    for table,plan in arguments['tables'].items():
        if table not in schema['properties'] or not isinstance(plan,dict) or set(plan)-{'sources','columns','filters','sort'}:
            raise ValueError('analysis_workbook_table')
        sources=plan.get('sources');columns=plan.get('columns');filters=plan.get('filters',[]);sort=plan.get('sort',[])
        if not isinstance(sources,list) or not 1<=len(sources)<=64 or not isinstance(columns,dict) or not 1<=len(columns)<=32:
            raise ValueError('analysis_workbook_budget')
        if not isinstance(filters,list) or len(filters)>16 or not isinstance(sort,list) or len(sort)>8:raise ValueError('analysis_workbook_budget')
        target=schema['properties'][table];target_columns=target['items']['properties']
        if set(columns)-set(target_columns):raise ValueError('analysis_workbook_columns')
        rows=[];seen=set()
        for source in sources:
            if not isinstance(source,dict) or set(source)-{'result_ref','path'} or not isinstance(source.get('result_ref'),str):raise ValueError('analysis_source')
            identity=(source['result_ref'],source.get('path',''))
            if identity[0] in partial_views:raise ValueError('analysis_partial_view')
            if identity in seen:raise ValueError('analysis_duplicate_source')
            seen.add(identity)
            if identity[0] not in results:
                raise ProjectionError('analysis_workbook_reference_missing',table=table)
            container=results[identity[0]]
            try:value=pointer(container,identity[1])
            except (KeyError,IndexError,TypeError,ValueError):
                raise ProjectionError('analysis_workbook_source_path_missing',table=table,source_field=identity[1],observed=container) from None
            if not isinstance(value,list) or len(rows)+len(value)>MAX_ROWS or any(not isinstance(row,dict) for row in value):raise ValueError('analysis_rows')
            rows.extend(value)
        projected=[]
        for row in rows:
            selected=True
            for condition in filters:
                try:keep=matches(row,condition)
                except (ValueError,KeyError,IndexError,TypeError):
                    raise ProjectionError('analysis_workbook_filter_input',table=table,source_field=condition.get('field'),observed=row) from None
                if not keep:selected=False;break
            if not selected:continue
            new={}
            for name,column in columns.items():
                if not isinstance(column,dict) or set(column)-{'field','truncate'} or 'field' not in column:raise ValueError('analysis_workbook_column')
                try:value=field_value(row,column['field'])
                except (KeyError,IndexError,TypeError,ValueError):
                    raise ProjectionError('analysis_workbook_field_missing',table=table,column=name,source_field=column['field'],observed=row) from None
                if 'truncate' in column:
                    maximum=column['truncate']
                    if type(maximum) is not int or not 1<=maximum<=4096 or not isinstance(value,str):raise ValueError('analysis_workbook_truncation')
                    value=value[:maximum]
                types=target_columns[name].get('type');types=[types] if isinstance(types,str) else types
                if isinstance(value,str) and ('number' in types or 'integer' in types) and 'string' not in types:
                    try:numeric=number(value)
                    except ValueError:
                        raise ProjectionError('analysis_workbook_numeric_mapping',table=table,column=name,source_field=column['field'],observed=row,expected_type=types) from None
                    if numeric==numeric.to_integral_value():value=int(numeric)
                    else:
                        value=float(numeric)
                        if Decimal(str(value))!=numeric:raise ValueError('analysis_workbook_lossy_number')
                if type(value) not in (str,int,float,bool,type(None)):raise ValueError('analysis_workbook_cell')
                new[name]=value
            projected.append(new)
            if len(projected)>min(MAX_ROWS,target.get('maxItems',MAX_ROWS)):raise ValueError('analysis_workbook_row_budget')
        for key in sort:
            if not isinstance(key,dict) or set(key)-{'column','descending'} or key.get('column') not in columns or type(key.get('descending',False)) is not bool:
                raise ValueError('analysis_sort')
        try:
            for key in reversed(sort):projected.sort(key=lambda row:(row[key['column']] is None,row[key['column']]),reverse=key.get('descending',False))
        except TypeError:raise ValueError('analysis_sort_type') from None
        output[table]=projected
    if len(json.dumps(output,allow_nan=False).encode())>256*1024:raise ValueError('analysis_output_budget')
    return output
