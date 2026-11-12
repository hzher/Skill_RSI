import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from skillrl.pastbench_campaign import main, tasks_complete


class CampaignRecoveryTests(unittest.TestCase):
    def test_task_complete_family_is_retained_despite_failed_skill_review_and_zero_reward(self):
        summary = {'attempts_finished': True, 'complete': False, 'episodes': [
            {'execution_complete': True, 'objective': {'score': 0}}]}
        self.assertTrue(tasks_complete(summary))
        summary['episodes'][0]['objective']['score'] = None
        self.assertFalse(tasks_complete(summary))

    def test_failed_transport_family_is_retained_and_complete_low_reward_attempt_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'upstream'
            family = source / 'self-evolve-tasks-v2/memory/example'
            family.mkdir(parents=True)
            (family / 'family.yaml').write_text('{}')
            config = root / 'config.json'
            config.write_text(json.dumps({'family_transport_attempts': 3}))
            output = root / 'output'
            called = []

            class Process:
                pid = 123

                def __init__(self, argv, **kwargs):
                    attempt = Path(argv[argv.index('--output') + 1])
                    index = len(called)
                    called.append(attempt)
                    if index == 0:
                        (attempt / 'transport-exhausted.json').write_text('{"transient":true}')
                    (attempt / 'family-summary.json').write_text(json.dumps({
                        'episodes': [{'task_id': 'sample', 'execution_complete': index > 0,
                                      'objective': {'score': 0 if index > 0 else None}}],
                        'attempts_finished': index > 0, 'passed': 0, 'known_scores': int(index > 0),
                        'complete': index > 0}))

                def wait(self):
                    return 0

            argv = ['campaign', '--source', str(source), '--config', str(config),
                    '--output', str(output), '--workers', '1']
            with patch('sys.argv', argv), patch('skillrl.pastbench_campaign.subprocess.Popen', Process), \
                 patch('skillrl.pastbench_campaign.subprocess.check_output', return_value='sha'), \
                 patch('skillrl.pastbench_campaign.time.sleep'), contextlib.redirect_stdout(io.StringIO()):
                main()
            result = json.loads((output / 'families/memory/example/worker-result.json').read_text())
            self.assertEqual(len(called), 2)
            self.assertEqual(result['selected_attempt'], 1)
            self.assertEqual(result['passed'], 0)
            self.assertTrue(result['complete'])
            self.assertTrue((called[0] / 'transport-exhausted.json').exists())
            self.assertEqual(json.loads((output / 'campaign-summary.json').read_text())['known_scores'], 1)
