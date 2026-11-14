import unittest
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from training.source_api import check_call, check_postprocess_alias, parse_source, MAX_SOURCE_BYTES, SOURCE_INPUTS, report_current


class SourceAPITests(unittest.TestCase):
    def test_source_inspection_does_not_execute_imports_defaults_or_body(self):
        raw = b'import no_such_framework\ndef run(x, *, option=execute_untrusted()):\n raise RuntimeError("never executed")\n'
        result = check_call(raw, "run", 1, ("option",))
        self.assertFalse(result["asynchronous"])
        self.assertEqual(result["parameters"][1]["kind"], "KEYWORD_ONLY")

    def test_missing_required_parameter_and_positional_only_keyword_are_rejected(self):
        for raw in (b'def run(x, new_required, *, option=None): pass', b'def run(x, option, /): pass'):
            with self.assertRaises(ValueError): check_call(raw, "run", 1, ("option",))

    def test_async_boundary_duplicate_symbols_and_missing_methods_are_rejected(self):
        with self.assertRaises(ValueError): check_call(b'async def run(x): pass', "run", 1)
        with self.assertRaises(ValueError): check_call(b'def run(x): pass\ndef run(x): pass', "run", 1)
        with self.assertRaises(ValueError): check_call(b'class Manager: pass', "Manager.run", 1)

    def test_worker_postprocess_alias_must_point_at_the_inspected_module_and_symbol(self):
        raw = b'from verl.experimental.agent_loop.agent_loop import AgentLoopWorker as _AgentLoopWorker\n_postprocess = _AgentLoopWorker._postprocess\n'
        self.assertEqual(check_postprocess_alias(raw)["target"], "AgentLoopWorker._postprocess")
        for bad in (raw.replace(b'._postprocess\n', b'.different\n'), raw.replace(b'from verl.', b'from other.')):
            with self.assertRaises(ValueError): check_postprocess_alias(bad)

    def test_oversized_sources_are_rejected_before_parsing(self):
        with self.assertRaises(ValueError): parse_source(b' ' * (MAX_SOURCE_BYTES + 1))

    def test_changed_adapter_bytes_invalidate_a_previous_source_report(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in SOURCE_INPUTS:
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(b'unit source fixture')
            (root / 'configs/mainline_sources.lock.json').write_text(json.dumps({'sources': {
                'verl_recipe': {'commit': 'a' * 40}, 'verl': {'commit': 'b' * 40}}}))
            members = ('nemo_gym/agent_loop.py', 'nemo_gym/dataset.py', 'nemo_gym/REQUIRED_VERL.txt',
                       'verl/experimental/agent_loop/agent_loop.py', 'verl/trainer/ppo/ray_trainer.py')
            inputs = [{'path': member, 'sha256': hashlib.sha256(member.encode()).hexdigest()} for member in members]
            report = {"schema_version": "pinned-training-source-api-v1", "inspection_completed": True,
                      "static_api_checks_passed": True, "checks": [{"passed": True}],
                      "git_inputs": inputs,
                      "source_sha256": {n: hashlib.sha256((root / n).read_bytes()).hexdigest() for n in SOURCE_INPUTS}}
            with patch('training.source_api.pinned_blob', side_effect=lambda root, source, member:
                       (member.encode(), next(item for item in inputs if item['path'] == member))):
                self.assertTrue(report_current(root, report))
                changed = {**report, 'git_inputs': [dict(item) for item in inputs]}
                changed['git_inputs'][0]['git_blob'] = '0' * 40
                self.assertFalse(report_current(root, changed))
                (root / SOURCE_INPUTS[1]).write_bytes(b'changed adapter')
                self.assertFalse(report_current(root, report))
