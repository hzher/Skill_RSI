"""Atomic SkillOS-style text operations with stable IDs and versioned delivery."""
from pathlib import Path
import copy,hashlib,json,re
from .common import atomic,canonical,digest,immutable,load

class InvalidOperations(ValueError):pass

def parse_skill(content):
 if not isinstance(content,str) or not content.startswith('---\n') or len(content)>100000:raise InvalidOperations('Skill must be a bounded Markdown string with frontmatter')
 parts=content.split('\n---',2)
 if len(parts)<2:raise InvalidOperations('Missing frontmatter close')
 header=parts[0][4:];body='\n---'.join(parts[1:]).lstrip('\r\n');fields={}
 for line in header.splitlines():
  if not line.strip():continue
  k,sep,v=line.partition(':');k=k.strip();v=v.strip()
  if not sep or k not in ['name','description'] or k in fields or not v:raise InvalidOperations('Only name/description string frontmatter supported')
  if v.startswith('"'):
   try:v=json.loads(v)
   except Exception as e:raise InvalidOperations('Invalid quoted frontmatter') from e
  elif v.startswith("'") and v.endswith("'"):v=v[1:-1].replace("''", "'")
  if not isinstance(v,str) or not v.strip():raise InvalidOperations('Empty frontmatter')
  fields[k]=v
 if set(fields)!={'name','description'} or not body.strip():raise InvalidOperations('Missing skill metadata/body')
 if re.search(r'!`[^`]+`',body):raise InvalidOperations('Dynamic command injection is outside plain-Markdown skill contract')
 return fields,body

def render(content,slug):
 fields,body=parse_skill(content)
 return '---\nname: '+slug+'\ndescription: '+json.dumps(fields['description'],ensure_ascii=False)+'\n---\n'+body

class SkillLibrary:
 def __init__(self,root,sequence_id):
  self.root=Path(root);self.sequence_id=sequence_id;self.root.mkdir(parents=True,exist_ok=True)
  if not (self.root/'state.json').exists():atomic(self.root/'state.json',{'sequence_id':sequence_id,'next_id':1,'active':{},'processed':{}})
  if self.state()['sequence_id']!=sequence_id:raise InvalidOperations('Sequence identity mismatch')
 def state(self):return load(self.root/'state.json')
 def apply(self,task_id,ops):
  if not isinstance(ops,list) or len(ops)>8:raise InvalidOperations('Operations must be a list of at most eight')
  state=self.state();fingerprint=digest(ops)
  if task_id in state['processed']:
   prev=state['processed'][task_id]
   if prev['operations_sha256']!=fingerprint:raise InvalidOperations('Conflicting operation replay')
   return prev
  updated=copy.deepcopy(state);applied=[];blobs={}
  for op in ops:
   if not isinstance(op,dict) or set(op)!={'name','arguments'} or not isinstance(op['arguments'],dict):raise InvalidOperations('Exact operation envelope required')
   name=op['name'];a=op['arguments'];title=a.get('skill_name')
   if not isinstance(title,str) or not title.strip() or len(title)>128:raise InvalidOperations('Invalid skill_name')
   active=updated['active']
   if name=='new_skill_insert':
    if set(a)!={'skill_name','content'} or title in active:raise InvalidOperations('Insert identity or fields')
    content=a['content'];parse_skill(content)
    sid=f"{self.sequence_id}:skill-{updated['next_id']:04d}";slug='t2-'+hashlib.sha256(self.sequence_id.encode()).hexdigest()[:10]+f"-{updated['next_id']:04d}"
    updated['next_id']+=1;entry={'skill_id':sid,'name':title,'native_slug':slug,'created_task':task_id}
   elif name=='skill_update':
    if title not in active or not set(a)<={'skill_name','new_name','new_content'} or not any(k in a for k in ['new_name','new_content']):raise InvalidOperations('Update target or fields')
    entry=active.pop(title)
    if 'new_content' in a:content=a['new_content']
    elif ('raw_versions',entry['raw_sha256']) in blobs:content=blobs[('raw_versions',entry['raw_sha256'])].decode()
    else:content=(self.root/'raw_versions'/(entry['raw_sha256']+'.md')).read_text()
    parse_skill(content)
    new_title=a.get('new_name',title)
    if not isinstance(new_title,str) or not new_title.strip() or len(new_title)>128 or new_title in active:raise InvalidOperations('Rename collision')
    title=new_title
   elif name=='skill_delete':
    if set(a)!={'skill_name'} or title not in active:raise InvalidOperations('Delete target or fields')
    entry=active.pop(title);applied.append({'op':name,'skill_id':entry['skill_id'],'name':title});continue
   else:raise InvalidOperations('Unknown operation')
   raw=content.encode();rendered=render(content,entry['native_slug']).encode();rh=hashlib.sha256(raw).hexdigest();dh=hashlib.sha256(rendered).hexdigest()
   blobs[('raw_versions',rh)]=raw;blobs[('delivery_versions',dh)]=rendered
   entry.update(name=title,raw_sha256=rh,delivery_sha256=dh,updated_task=task_id)
   active[title]=entry;applied.append({'op':name,**entry})
  result={'task_id':task_id,'operations_sha256':fingerprint,'applied_ops':applied,'active':list(updated['active'].values())}
  updated['processed'][task_id]=result
  for (folder,h),raw in blobs.items():immutable(self.root/folder/(h+'.md'),raw)
  atomic(self.root/'state.json',updated);immutable(self.root/'commits'/(hashlib.sha256(task_id.encode()).hexdigest()+'.json'),canonical(result))
  return result
 def model_view(self):
  return [{'skill_name':e['name'],'content':(self.root/'raw_versions'/(e['raw_sha256']+'.md')).read_text()} for e in self.state()['active'].values()]
 def snapshot(self,destination):
  dest=Path(destination);dest.mkdir(parents=True,exist_ok=False);dest.chmod(0o755);entries=list(self.state()['active'].values())
  for e in entries:
   p=dest/e['native_slug'];p.mkdir(mode=0o755);f=p/'SKILL.md';f.write_bytes((self.root/'delivery_versions'/(e['delivery_sha256']+'.md')).read_bytes());f.chmod(0o644)
  return {'sequence_id':self.sequence_id,'active_ids':[e['skill_id'] for e in entries],'entries':entries,'path':str(dest)}
