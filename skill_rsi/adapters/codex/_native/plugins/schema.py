"""Small structural schema checker shared by independent plugins."""
def schema_issues(value, schema, path='$'):
    issues=[]
    kind=schema.get('type')
    valid={'object':isinstance(value,dict),'array':isinstance(value,list),'string':isinstance(value,str),
           'integer':isinstance(value,int) and not isinstance(value,bool),
           'number':isinstance(value,(int,float)) and not isinstance(value,bool),'boolean':isinstance(value,bool),
           'null':value is None}
    if isinstance(kind,str) and kind in valid and not valid[kind]:
        return [path+' must be '+kind]
    if 'enum' in schema and value not in schema['enum']:issues.append(path+' must be one of '+repr(schema['enum']))
    if isinstance(value,dict):
        props=schema.get('properties',{})
        issues.extend(path+'/'+key+' is required' for key in schema.get('required',[]) if key not in value)
        if schema.get('additionalProperties') is False:
            issues.extend(path+'/'+key+' is not accepted by the declared schema' for key in value if key not in props)
        for key,item in value.items():
            if key in props:issues.extend(schema_issues(item,props[key],path+'/'+key))
    if isinstance(value,list) and isinstance(schema.get('items'),dict):
        for i,item in enumerate(value):issues.extend(schema_issues(item,schema['items'],path+'/'+str(i)))
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        if 'minimum' in schema and value<schema['minimum']:issues.append(path+' below minimum '+str(schema['minimum']))
        if 'maximum' in schema and value>schema['maximum']:issues.append(path+' above maximum '+str(schema['maximum']))
    return issues
