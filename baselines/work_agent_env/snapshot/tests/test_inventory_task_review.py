"""Public source review cannot execute inputs, read answers or activate a task."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from training import inventory_task_review as review
from workenv.core import save


class InventoryReviewTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()

    def test_unlisted_answers_and_verified_paths_are_refused_before_io(self):
        for member in ('../reference/out_of_stock_report.txt','groundtruth/out_of_stock_report.txt','tasks/verified/task.md','evaluation/helper.py'):
            with self.subTest(member=member),self.assertRaises(ValueError):review.public_blob(self.root,member)
        self.assertEqual(list(self.root.iterdir()),[])

    def test_pin_drift_and_changed_public_bytes_refuse_git_or_review(self):
        path=self.root/'configs/mainline_sources.lock.json'
        save(path,{'sources':{'toolathlon_gym':{'commit':'0'*40}}})
        with patch.object(review,'pinned_blob') as read:
            with self.assertRaises(ValueError):review.public_blob(self.root,'task_config.json')
            read.assert_not_called()
        save(path,{'sources':{'toolathlon_gym':{'commit':review.PIN}}})
        with patch.object(review,'pinned_blob',return_value=(b'changed',{})):
            with self.assertRaises(ValueError):review.public_blob(self.root,'task_config.json')

    def test_analysis_never_executes_source_and_records_reference_and_clock(self):
        sentinel=self.root/'must-not-exist'
        pre="from pathlib import Path\nPath("+repr(str(sentinel))+ ").write_text('executed')\nquery='DELETE FROM email.messages'\n"
        evaluator="DB_CONFIG={'port':5432}\nOOS_PRODUCT_IDS=[1,2]\ndef evaluate(groundtruth_workspace):\n    return datetime.now(), groundtruth_workspace, 'SELECT * FROM gcal.events'\n"
        facts=review.script_facts(pre,evaluator)
        self.assertFalse(sentinel.exists());self.assertTrue(facts['reference_workspace_required'])
        self.assertEqual(facts['preprocess_delete_tables'],['email.messages']);self.assertEqual(facts['evaluator_read_tables'],['gcal.events'])
        self.assertEqual(facts['public_evaluator_inventory_constant_count'],2);self.assertEqual(facts['clock_calls'],['datetime.now'])

    def fixture(self):
        for name in review.SOURCES:
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('SENDER="vip@example.com"\n' if name.endswith('email_pg_native.py') else '# source fixture\n')
        blobs={'task_config.json':json.dumps({'needed_mcp_servers':['woocommerce','emails','google_calendar','filesystem'],
            'needed_local_tools':[],'meta':{'runtime_activation':True}}).encode(),'docs/task.md':b'public source fixture',
            'preprocess/main.py':b"sql='DELETE FROM email.messages'\n",'evaluation/main.py':b"DB_CONFIG={'port':5432}\nOOS_PRODUCT_IDS=[1]\n"}
        return patch.object(review,'public_blob',side_effect=lambda root,name:(blobs[name],{'path':review.BASE+name}))

    def test_config_metadata_cannot_enable_runtime_or_verify_source_completeness(self):
        with self.fixture():
            result=review.inspect(self.root)
        for key in ('runtime_activation','training_admission','source_completeness_verified','resource_ownership_verified','original_scripts_executed'):
            self.assertIs(result[key],False)
        self.assertFalse(result['task_sender_matches_existing_default'])
        self.assertTrue(result['port_compatible_with_current_5432_slots'])

    def test_forged_activation_or_reference_read_is_rejected_by_recomputation(self):
        with self.fixture():
            original=review.inspect(self.root);path=self.root/'review.json';save(path,original)
            self.assertEqual(review.audit_review(self.root,path)['public_files_reviewed'],4)
            for key in ('runtime_activation','reference_files_read','training_admission'):
                changed=deepcopy(original);changed[key]=True;save(path,changed)
                with self.assertRaises(ValueError):review.audit_review(self.root,path)


if __name__=='__main__':unittest.main()
