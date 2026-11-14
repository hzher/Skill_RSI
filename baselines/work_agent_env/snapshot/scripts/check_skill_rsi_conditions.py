"""Audit retained comparison conditions without model calls, services or task replay."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from integrations.skill_rsi_conditions import compare_retained_conditions
from integrations.skill_rsi_contract import RSIError
from workenv.core import no_links,save

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--comparison-id',required=True)
    args=parser.parse_args(argv)
    try:
        result=compare_retained_conditions(ROOT,args.comparison_id)
        path=ROOT/'evidence/skill_rsi/conditions'/(result['request_id']+'.json')
        no_links(path,ROOT);save(path,result)
        print(json.dumps({'binding_audit_passed':True,'dimensions':{key:row['status'] for key,row in result['dimensions'].items()},
            'initial_state_equality_attested':False,'selection_eligible':False,'model_http_requests':0}))
        return 0
    except Exception as error:
        print(json.dumps({'binding_audit_passed':False,'error':error.code if isinstance(error,RSIError) else 'conditions_input_invalid',
                          'initial_state_equality_attested':False,'selection_eligible':False,'model_http_requests':0}))
        return 2

if __name__=='__main__':raise SystemExit(main())
