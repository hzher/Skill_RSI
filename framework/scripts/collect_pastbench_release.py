"""Package whole-family reruns and frozen reused families, without model calls."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import csv,difflib,gzip,hashlib,io,json,shutil,tarfile,subprocess
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from skillrl.export_campaign import export_campaign

def read(p):return json.loads(Path(p).read_text())
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def write(p,v):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n')
def relative(p,root):return str(Path(p).relative_to(root))
def archive_files(destination,root,files):
 with tarfile.open(destination,'w:gz',compresslevel=6) as archive:
  for p in sorted(files):
   if p.is_symlink():raise ValueError('No symlink source evidence allowed')
   archive.add(p,arcname=relative(p,root),recursive=False)
def export(plan_file,base_plan_file,audit_file,destination):
 plan_file,base_plan_file,audit_file,destination=map(lambda p:Path(p).resolve(),(plan_file,base_plan_file,audit_file,destination))
 plan=read(plan_file);base=read(base_plan_file);progress=read(plan_file.with_name('progress.json'))
 completion=read(plan_file.with_name('completion.json'))
 if progress['finished_rerun_families']!=plan['rerun_families'] or not completion['sources_unchanged']:
  raise ValueError('All rerun families must stop and frozen sources must remain unchanged')
 destination.mkdir(parents=True,exist_ok=False);provenance=destination/'provenance';provenance.mkdir()
 audit=read(audit_file);expected={r['task_id']:r for r in audit['records']};origins=[];index=[];family_stats=[];differences=[];flag_checks=[]
 refs={f['family']:f for f in progress['families']};original_configs={f['family']:f for f in base['families']}
 for f in plan['families']:
  family=f['family'];slug=family.replace('/','__');source=Path(refs[family]['campaign']);family_root=source/'families'/family
  summary=read(family_root/'family-summary.json');campaign_plan=read(source/'campaign-plan.json')
  if len(summary['episodes'])!=f['total']:raise ValueError('Selected family inventory incomplete')
  exported=Path(str(source)+'-export')
  if not exported.exists():export_campaign(source,exported)
  target=destination/'families'/slug;target.mkdir(parents=True)
  for p in sorted(exported.rglob('*')):
   if not p.is_file():continue
   q=target/p.relative_to(exported);q.parent.mkdir(parents=True,exist_ok=True)
   if p.suffix=='.jsonl':
    q=q.with_suffix(q.suffix+'.gz')
    with p.open('rb') as src,gzip.open(q,'wb',compresslevel=6) as dst:shutil.copyfileobj(src,dst)
   else:shutil.copy2(p,q)
  config=read(f['config']);old=read(original_configs[family]['config'])
  if f['rerun'] and {k:v for k,v in config.items() if k!='persistence_restoration'}!=old:
   raise ValueError('Non-persistence configuration changed')
  write(target/'run-config.json',config)
  shutil.copy2(family_root/'family-summary.json',target/'original-family-summary.json')
  for audit_name in ('public-boundary-audit.json','subset-audit.json'):
   if (source/audit_name).exists():shutil.copy2(source/audit_name,target/audit_name)
  shutil.copy2(family_root/'upstream-manifest.yaml',target/'original-upstream-manifest.yaml')
  if (family_root/'effective-persistence-specifications.json').exists():shutil.copy2(family_root/'effective-persistence-specifications.json',target/'effective-persistence-specifications.json')
  stats=Counter(planned=f['total']);known_ids=[]
  with (exported/'all-trajectories.jsonl').open() as stream:
   for line in stream:
    row=json.loads(line);tid=row['task_id'];known_ids.append(tid);e=row.get('execution');score=(row.get('objective') or {}).get('score');r=expected[tid]
    if e:
     flags={k:e['persistence_flags'][k] for k in r['expected_original_with_flags']}
     wanted=r['expected_original_with_flags'] if r['bucket']=='control' else r['actual_flags']
     if wanted is None:wanted=flags  # Original unscored task remains unscored, never backfilled.
     if flags!=wanted:raise ValueError('Unexpected persistence flags: '+tid)
     if r['bucket']!='control' and not any(flags.values()):raise ValueError('Ordinary collection stage unexpectedly disabled')
     flag_checks.append({'family':family,'task_id':tid,'bucket':r['bucket'],'flags':flags,'matches_requested_profile':True})
     if f['rerun'] and flags!=r['actual_flags']:differences.append({'family':family,'task_id':tid,'bucket':r['bucket'],'before':r['actual_flags'],'after':flags})
    stats['scored']+=score is not None;stats['passed']+=score==1;stats['failed']+=score==0;stats['unknown']+=score is None;stats['execution_complete']+=bool(e and e.get('complete'));stats['successful_trajectory']+=bool(row['successful_trajectory'])
    trace_path=target/'traces'/family/(tid+'.md')
    index.append({'family':family,'task_id':tid,'episode':row['episode'],'bucket':r['bucket'],'stage_origin':'rerun' if f['rerun'] else 'reused','score':score,'task_score':(row.get('objective') or {}).get('details',{}).get('task_score'),'execution_complete':bool(e and e.get('complete')),'successful_trajectory':row['successful_trajectory'],'persistence_flags':e.get('persistence_flags') if e else None,'trace':relative(trace_path,destination),'family_trajectories':relative(target/'all-trajectories.jsonl.gz',destination),'source_campaign':str(source),'original_report':row['report'],'policy_gradient_eligible':False})
  if len(known_ids)!=len(set(known_ids)) or set(known_ids)!=set(f['task_ids']):raise ValueError('Selected task IDs differ from frozen plan')
  # Preserve exact actor wire bytes, including unsuccessful responses, as a separate evidence archive.
  raw_files=[]
  for row in summary['episodes']:
   ep=Path(row['report']).parent
   raw_files.extend(p for p in (ep/'episodes'/row['task_id']/'actor/wire').rglob('*') if p.is_file())
  if raw_files:archive_files(target/'native-wire-receipts.tar.gz',family_root,raw_files)
  origin={'family':family,'origin':'rerun' if f['rerun'] else 'reused','source_campaign':str(source),'source_summary_sha256':sha(family_root/'family-summary.json'),'campaign_plan_sha256':sha(source/'campaign-plan.json'),'executor':campaign_plan['config']['models']['executor'],'runtime_source_sha256':campaign_plan['runtime_source_sha256']}
  old_progress=read(base_plan_file.with_name('progress.json'))
  old_stats=next(x for x in old_progress['families'] if x['family']==family)
  origins.append(origin);family_stats.append({'family':family,'origin':origin['origin'],**dict(stats),'previous_passed':old_stats['passed'],'previous_scored':old_stats['known'],'pass_rate':stats['passed']/stats['scored'] if stats['scored'] else None})
  print(json.dumps({'event':'packaged_family','family':family,**dict(stats)}),flush=True)
 if len(index)!=204 or len({r['task_id'] for r in index})!=204:raise ValueError('204 unique task positions required')
 if len(differences)!=7 or any(r['bucket']!='control' for r in differences):raise ValueError('Only seven requested control permissions may differ')
 write(destination/'source-map.json',origins);write(destination/'families.json',family_stats);write(provenance/'control-switch-changes.json',differences);write(provenance/'persistence-verification.json',flag_checks)
 for name,rows in [('trajectories-index.jsonl',index),('successful-trajectories-index.jsonl',[r for r in index if r['successful_trajectory']]),('task-passed-index.jsonl',[r for r in index if r['score']==1]),('unscored-index.jsonl',[r for r in index if r['score'] is None])]:
  (destination/name).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
 with (destination/'tasks.csv').open('w') as stream:
  cols=('family','task_id','bucket','stage_origin','score','task_score','execution_complete','successful_trajectory','trace');writer=csv.DictWriter(stream,fieldnames=cols,extrasaction='ignore');writer.writeheader();writer.writerows(index)
 for name,path in [('rerun-plan.json',plan_file),('previous-plan.json',base_plan_file),('completion.json',plan_file.with_name('completion.json')),('previous-persistence-audit.json',audit_file)]:shutil.copy2(path,provenance/name)
 for name in ('switch-audit.json','lineage-validation.json'):
  if (plan_file.parent/name).exists():shutil.copy2(plan_file.parent/name,provenance/name)
 source=Path(plan['source'])
 for license_name in ('LICENSE','NOTICE'):
  try:data=subprocess.check_output(['git','-C',str(source),'show',plan['upstream_commit']+':'+license_name],stderr=subprocess.DEVNULL)
  except subprocess.CalledProcessError:continue
  (provenance/('UPSTREAM_'+license_name)).write_bytes(data)
 archive_files(provenance/'repaired-benchmark.tar.gz',source,[source/k for k in plan['source_files_sha256']])
 after=Path(next(r['campaign'] for r in refs.values() if r['origin']=='rerun')).parents[1]/'framework'
 before=Path(next(r['campaign'] for r in refs.values() if r['origin']=='reused')).parents[1]/'framework'
 for label,root in [('before',before),('after',after)]:
  if not root.exists():raise ValueError('Frozen framework evidence missing: '+str(root))
  archive_files(provenance/f'framework-{label}.tar.gz',root,[p for p in root.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'])
 patch=[]
 for k in sorted(plan['framework_switch_changes']):
  a=(before/k).read_text().splitlines(keepends=True) if (before/k).exists() else []
  b=(after/k).read_text().splitlines(keepends=True)
  patch.extend(difflib.unified_diff(a,b,fromfile='before/'+k,tofile='after/'+k))
 (provenance/'persistence-only-framework.patch').write_text(''.join(patch))
 totals={key:sum(f.get(key,0) for f in family_stats) for key in ('planned','scored','passed','failed','unknown','execution_complete','successful_trajectory')}
 all_passed=sum(f['passed']==f['planned'] and f['scored']==f['planned'] for f in family_stats)
 manifest={'schema':'skillrl-merged-collection-release-v1','created_at':datetime.now(timezone.utc).isoformat(),'profile':plan['profile'],'selection':plan['selection'],'totals':totals,'all_passed_families':all_passed,'rerun_families':4,'rerun_tasks':32,'reused_families':22,'reused_tasks':172,'control_flag_changes':7,'persistence_verified_complete_executions':len(flag_checks),'training_updates':0,'policy_gradient_eligible':False,'scope':'Merged versioned experience collection, not one independent full-suite run or paper replication','files_sha256':{str(p.relative_to(destination)):sha(p) for p in destination.rglob('*') if p.is_file()}}
 write(destination/'manifest.json',manifest)
 md=['# PAST-Bench 经验采集轨迹（2026-11-16）','',f"204 个任务位置；有效评分 {totals['scored']}，通过 {totals['passed']}，未通过 {totals['failed']}，未评分 {totals['unknown']}。已评分通过率 {totals['passed']/totals['scored']:.2%}；全题通过任务族 {all_passed}/26。",'',
 '## 采集配置与来源','',
 'cold、learn/reflection、evaluation 保留此前允许持久化的配置与合法状态继承；control 恢复固定上游基线的工具开闭，包含 EP01 I06 的 fresh/关闭条件。其余题面、评分器、fixture、业务工具、模型及预算没有改变。不是论文 shared-cold 复现，也没有运行配对关闭组。','',
 '4 个受影响任务族整族新跑 32 题，另外 22 族完整复用 2026-11-15 修订版的 172 个位置。不按奖励挑选任务或跨轮拼接同族。旧未知评分保持 null。此前被用户中止的两次 wider-scope 探针未选入本数据集。','',
 'EP01 I06 保留既有 history_recovery 题面/评分/ID，仅关闭持久化并恢复 fresh 分支；因此该位置不是原版题目语义，低分照实保留。','',
 '## 阅读与提炼技能','',
 '- `trajectories-index.jsonl` / `tasks.csv`：204 题索引、阶段、分数、来源及可读轨迹路径。',
 '- `task-passed-index.jsonl`：原 evaluator 判定通过；`successful-trajectories-index.jsonl` 另要求执行、流水线与原生响应完整，门槛更严格。',
 '- `families/<组名>/traces/`：实际初始提示、工具调用、最终回答及评分回执。',
 '- `families/<组名>/*.jsonl.gz`：完整轨迹、成功原生轮次与技能版本。用 Python gzip 或 zcat 读取；原生模型轮次独立于评价元数据，避免将私有答案混入技能提炼输入。',
 '- `families/<组名>/native-wire-receipts.tar.gz`：原始 actor 请求/响应字节及失败回执。',
 '- `source-map.json`：每个任务族的新跑/复用来源和源码身份；`provenance/`：冻结题库、修复前后框架、仅持久化修改补丁及开关核验。','',
 '成功轨迹可用于整理技能候选；存在成功消费者不代表因果增益已验证。control 的低分与普通业务失败分开解读；接口或评分异常不当作模型负例。没有 Generator 自身训练采样身份，不可作为直接策略梯度样本。','',
 '## 逐族结果','', '旧通过数只作同族历史参照；整族独立重跑会有采样差异，分数变化不能全部归因于 control 开关。','', '|任务族|来源|有效评分/计划|旧通过|当前通过|当前通过率|','|---|---|---:|---:|---:|---:|']
 for f in family_stats:md.append(f"|{f['family']}|{f['origin']}|{f['scored']}/{f['planned']}|{f['previous_passed']}|{f['passed']}|{f['pass_rate']:.2%}|")
 (destination/'README.md').write_text('\n'.join(md)+'\n')
 # Include the human report in the digest inventory, avoiding a self-hash cycle.
 manifest['files_sha256']['README.md']=sha(destination/'README.md');write(destination/'manifest.json',manifest)
 print(json.dumps({'event':'release_exported','destination':str(destination),'totals':totals},ensure_ascii=False))
 return manifest
if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--plan',type=Path,required=True);parser.add_argument('--previous-plan',type=Path,required=True);parser.add_argument('--audit',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
 export(args.plan,args.previous_plan,args.audit,args.output)
