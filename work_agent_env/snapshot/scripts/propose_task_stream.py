"""Build inspectable related-task proposals from recorded Executor experiences."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from integrations.benchmark_tasks import load_task_set
from integrations.benchmark_tasks.contracts import digest
from integrations.benchmark_tasks.store import TaskStore,save
from integrations.experience import ExperienceStore
from integrations.skill_library import SkillLibrary
from training.task_stream import propose_edges


def reserved_prompt_objects(tree,index):
    if tree['sha']!=index['root_git_tree'] or tree.get('truncated') is not False:
        raise ValueError('Independent evaluation tree metadata differs from its pinned inventory')
    identities=set(index['task_ids'])
    result=set()
    for row in tree['tree']:
        path=row['path'].removeprefix('tasks/finalpool/')
        task_id,separator,relative=path.partition('/')
        if row['type']=='blob' and separator and task_id in identities and relative in ('docs/task.md','task.md'):
            result.add(row['sha'])
    if not result:raise ValueError('No reserved prompt object identities found')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execution',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--count',type=int,default=20)
    parser.add_argument('--verified-tree',type=Path,help='Pinned Git tree metadata only; no evaluation task bodies are loaded')
    args=parser.parse_args()
    if args.output.exists():raise ValueError('Do not overwrite proposal records')
    registration=TaskStore(ROOT).register('toolathon-gym')
    tasks=load_task_set(registration['task_set'])['tasks']
    rows=json.loads(args.execution.read_text())['results']
    store=ExperienceStore(ROOT/'.runtime/experience')
    experiences=[]
    feedback_by_experience={}
    for row in rows:
        identity=(row.get('experience') or {}).get('id') or row.get('experience_id')
        if not identity:continue
        experiences.append((identity,store.get(identity),store.view(identity)))
        if row.get('index') is not None:
            task_directory=Path(row['evidence_directory']) if row.get('evidence_directory') else args.execution.parent/'tasks'/f"{row['index']:02d}"
            feedback_path=task_directory/'selfjudge.json'
            if feedback_path.is_file():
                feedback=json.loads(feedback_path.read_text())
                feedback_by_experience[identity]={'role':'selfjudge','model_revision':feedback['model_revision'],'verdict':feedback['parsed']}
    reserved_blobs=None
    if args.verified_tree:
        tree=json.loads(args.verified_tree.read_text())
        index=json.loads((ROOT/'evaluation/toolathlon_verified.tasks.json').read_text())
        reserved_blobs=reserved_prompt_objects(tree,index)
    result=propose_edges(experiences,tasks,evaluation_manifest=json.loads((ROOT/'evaluation/toolathlon_verified.manifest.json').read_text()),
        parent=SkillLibrary(ROOT/'.runtime/skill_library').empty_version,limit=args.count,reserved_prompt_blobs=reserved_blobs)
    if reserved_blobs:
        result.update(reserved_prompt_objects=len(reserved_blobs),reserved_tree_sha256=tree['sha'],evaluation_bodies_read=False)
    for proposal in result['proposals']:
        pair=proposal['pair']
        if pair['experience_id'] in feedback_by_experience:
            pair['selfjudge_feedback']=feedback_by_experience[pair['experience_id']]
            proposal['pair_sha256']=digest(pair)
            proposal['recorded_selfjudge_feedback_attached']=True
    save(args.output,result)
    print(json.dumps({'proposals':len(result['proposals']),'rejected':len(result['rejected']),
                      'training_admitted':False,'output':str(args.output)},ensure_ascii=False))


if __name__=='__main__':main()
