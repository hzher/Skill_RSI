"""Recompute RSI evidence checks offline; no model credentials or running services needed."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from integrations.skill_rsi_audit import audit_retained
from integrations.skill_rsi_contract import RSIError
from workenv.core import no_links,save


def main():
    try:
        result=audit_retained(ROOT)
        target=ROOT/'evidence/skill_rsi/retained_inputs_audit.json'
        no_links(target,ROOT);save(target,result)
        print(json.dumps({'passed':result['passed'],'execution_receipts_checked':result['execution_receipts_checked'],
            'retained_checks':sum(len(row.get('checks',{})) for row in result['runs']),
            'comparison_checks':sum(len(row.get('checks',{})) for row in result['comparisons']),
            'model_http_requests_started_by_audit':0}))
        return 0 if result['passed'] else 2
    except (RSIError,OSError,ValueError) as exc:
        print(json.dumps({'passed':False,'error':exc.code if isinstance(exc,RSIError) else 'audit_local_input_unavailable'}))
        return 2


if __name__=='__main__':raise SystemExit(main())
