"""Fix the observed Notion schema/storage contract and partial-update semantics."""
import json
from pathlib import Path
import shutil


def replace(path, old, new):
    text = path.read_text()
    if new in text:
        return
    if text.count(old) != 1:
        raise RuntimeError(f'Unexpected source: {path}: {old[:60]}')
    path.write_text(text.replace(old, new))


root = Path('/opt/local_servers/notion-mcp-server')
source = root/'src/openapi-mcp-server/client/pg-client.ts'
shutil.copy2('/tmp/notion_contract.ts', source.with_name('notion_contract.ts'))
replace(source, "const { Pool } = pg", "import { normalizeParams, pageList, matchesFilter, sortPages } from './notion_contract.js'\n\nconst { Pool } = pg")
replace(source, '    const operationId = operation.operationId',
        "    params = normalizeParams(params, operation.operationId || '')\n    const operationId = operation.operationId")
text = source.read_text()
start = text.index('    const { database_id, filter, sorts } = params', text.index('private async queryDatabase'))
end = text.index('\n  // 8. retrieve-a-page', start)
text = text[:start] + '''    const { database_id, filter, sorts } = params
    const { rows } = await this.pool.query(
      `SELECT * FROM notion.pages WHERE parent->>'database_id' = $1 AND archived = false AND in_trash = false`,
      [database_id],
    )
    const selected = sortPages(rows.filter((row: any) => matchesFilter(row, filter)), sorts)
    return this.makeResponse(pageList(selected, params))
  }
''' + text[end:]
# Both page and database property PATCH operations must preserve unspecified keys.
text = text.replace('setClauses.push(`${field} = $${idx}`)',
                    "setClauses.push(field === 'properties' ? `properties = properties || $${idx}::jsonb` : `${field} = $${idx}`)")
text = text.replace('return this.makeResponse(this.wrapList(results))',
                    'return this.makeResponse(pageList(results, params))')
text = text.replace('return this.makeResponse(this.wrapList(formatted))',
                    'return this.makeResponse(pageList(formatted, params))')
source.write_text(text)

spec_path = root/'scripts/notion-openapi.json'
spec = json.loads(spec_path.read_text())
rich_text = {'type':'array','items':{'type':'object','properties':{
    'type':{'type':'string','enum':['text']},
    'text':{'type':'object','properties':{'content':{'type':'string'}},'required':['content']}},'required':['text']}}
page_properties = {'type':'object','description':'Map property names to Notion property value objects; title uses {type: title, title: rich-text array}.',
                   'properties':{'title':{'type':'object','properties':{'type':{'type':'string','enum':['title']},'title':rich_text},'required':['title']}},
                   'additionalProperties':{'type':'object'}}
for methods in spec['paths'].values():
    for operation in methods.values():
        if not isinstance(operation,dict):
            continue
        kind=operation.get('operationId')
        if kind not in {'post-page','patch-page'}:
            continue
        properties=operation['requestBody']['content']['application/json']['schema']['properties']
        properties['properties']=page_properties
        if kind=='post-page':
            properties['parent']={'type':'object','properties':{'page_id':{'type':'string'},'database_id':{'type':'string'}},
                                  'anyOf':[{'required':['page_id']},{'required':['database_id']}]}
            properties['children']={'type':'array','items':{'type':'object','properties':{'object':{'type':'string'},'type':{'type':'string'}},'additionalProperties':True}}
            properties['icon']={'type':['object','null'],'additionalProperties':True}
            properties['cover']={'type':['object','null'],'additionalProperties':True}
spec_path.write_text(json.dumps(spec,indent=2)+'\n')
print('Notion canonical schemas, JSON transport, partial updates, typed filters and pagination patched')
